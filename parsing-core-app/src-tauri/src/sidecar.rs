use crate::state::AppState;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

const HEALTH_INTERVAL_SECS: u64 = 3;
const MAX_HEALTH_FAILURES: u8 = 3;

pub async fn start_sidecar(_app: &tauri::AppHandle, state: Arc<Mutex<AppState>>) -> Result<(), String> {
    let port = {
        let mut s = state.lock().map_err(|e| e.to_string())?;
        if s.running {
            return Err("already running".into());
        }
        s.running = true;
        s.port
    };

    let parent_pid = std::process::id();
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let python = exe
        .parent()
        .ok_or_else(|| "missing app executable directory".to_string())?
        .join("python3");
    let child = Command::new(&python)
        .args([
            "--port",
            &port.to_string(),
            "--parent-pid",
            &parent_pid.to_string(),
            "--host",
            "127.0.0.1",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("failed to start {}: {}", python.display(), e))?;

    {
        let mut s = state.lock().map_err(|e| e.to_string())?;
        s.sidecar_child = Some(child);
        s.logs.push(format!("[sidecar] started on port {}", port));
    }

    Ok(())
}

pub async fn stop_sidecar(state: Arc<Mutex<AppState>>) -> Result<(), String> {
    let mut s = state.lock().map_err(|e| e.to_string())?;
    if let Some(mut child) = s.sidecar_child.take() {
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
