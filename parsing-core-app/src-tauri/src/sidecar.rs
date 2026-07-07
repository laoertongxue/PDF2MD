use crate::state::AppState;
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use tauri::Emitter;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

const HEALTH_INTERVAL_SECS: u64 = 3;
const MAX_HEALTH_FAILURES: u8 = 3;

pub fn find_free_port(start: u16) -> u16 {
    for port in start..start + 100 {
        if TcpListener::bind(("127.0.0.1", port)).is_ok() {
            return port;
        }
    }
    start
}

pub async fn start_sidecar(app: &tauri::AppHandle, state: Arc<Mutex<AppState>>) -> Result<(), String> {
    let port = {
        let mut s = state.lock().map_err(|e| e.to_string())?;
        if s.running {
            return Err("already running".into());
        }
        s.running = true;
        s.port
    };

    let parent_pid = std::process::id();
    let sidecar_command = app
        .shell()
        .sidecar("python3")
        .map_err(|e| e.to_string())?
        .args([
            "-m",
            "parsing_core.serve",
            "--port",
            &port.to_string(),
            "--parent-pid",
            &parent_pid.to_string(),
            "--host",
            "127.0.0.1",
        ]);

    let (mut rx, child) = sidecar_command.spawn().map_err(|e| e.to_string())?;

    {
        let mut s = state.lock().map_err(|e| e.to_string())?;
        s.sidecar_child = Some(child);
        s.logs.push(format!("[sidecar] started on port {}", port));
    }
    let app_clone = app.clone();
    let state_clone = state.clone();

    tokio::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let line_str = String::from_utf8_lossy(&line);
                    let mut s = state_clone.lock().unwrap();
                    s.logs.push(line_str.to_string());
                    if s.logs.len() > 100 {
                        s.logs.remove(0);
                    }
                    drop(s);
                    app_clone.emit("log", line_str.to_string()).ok();
                }
                CommandEvent::Stderr(line) => {
                    let line_str = String::from_utf8_lossy(&line);
                    let mut s = state_clone.lock().unwrap();
                    s.logs.push(format!("[stderr] {}", line_str));
                    drop(s);
                }
                CommandEvent::Terminated(_) => {
                    let mut s = state_clone.lock().unwrap();
                    s.running = false;
                    s.sidecar_child = None;
                    s.logs.push("[sidecar] terminated".into());
                    drop(s);
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(())
}

pub async fn stop_sidecar(state: Arc<Mutex<AppState>>) -> Result<(), String> {
    let mut s = state.lock().map_err(|e| e.to_string())?;
    if let Some(child) = s.sidecar_child.take() {
        child.kill().map_err(|e| e.to_string())?;
        s.logs.push("[sidecar] stopped".into());
    }
    s.running = false;
    Ok(())
}

pub async fn health_loop(app: tauri::AppHandle, state: Arc<Mutex<AppState>>) {
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(HEALTH_INTERVAL_SECS)).await;
        let port = state.lock().unwrap().port;
        let url = format!("http://127.0.0.1:{}/health", port);
        match reqwest::get(&url).await {
            Ok(r) if r.status().is_success() => {
                state.lock().unwrap().health_failures = 0;
            }
            _ => {
                let should_restart = {
                    let mut s = state.lock().unwrap();
                    s.health_failures += 1;
                    if s.health_failures >= MAX_HEALTH_FAILURES {
                        s.logs.push("[health] 3 failures, restarting sidecar...".into());
                        true
                    } else {
                        false
                    }
                };
                if should_restart {
                    let _ = stop_sidecar(state.clone()).await;
                    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                    let _ = start_sidecar(&app, state.clone()).await;
                }
            }
        }
    }
}
