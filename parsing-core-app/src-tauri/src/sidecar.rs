use crate::state::AppState;
use std::fs::{create_dir_all, OpenOptions};
use std::net::TcpListener;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

const HEALTH_INTERVAL_SECS: u64 = 3;
const HEALTH_STARTUP_GRACE_SECS: u64 = 60;
const MAX_HEALTH_FAILURES: u8 = 3;

pub fn reserve_loopback_port() -> std::io::Result<(TcpListener, u16)> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    let port = listener.local_addr()?.port();
    Ok((listener, port))
}

fn sidecar_command(script: &Path, port: u16, parent_pid: u32, health_token: &str) -> Command {
    let mut command = Command::new("/bin/bash");
    command.arg(script).args([
        "--port",
        &port.to_string(),
        "--parent-pid",
        &parent_pid.to_string(),
        "--host",
        "127.0.0.1",
        "--health-token",
        health_token,
    ]);
    command
}

pub fn child_exited(child: &mut std::process::Child) -> std::io::Result<bool> {
    Ok(child.try_wait()?.is_some())
}

pub fn terminate_child(child: &mut std::process::Child) -> std::io::Result<()> {
    if !child_exited(child)? {
        child.kill()?;
        child.wait()?;
    }
    Ok(())
}

async fn instance_is_healthy(client: &reqwest::Client, port: u16, token: &str) -> bool {
    let url = format!("http://127.0.0.1:{}/health", port);
    match client.get(url).header("X-PDF2MD-Health-Token", token).send().await {
        Ok(response) if response.status().is_success() => response
            .json::<serde_json::Value>()
            .await
            .ok()
            .and_then(|body| body.get("instance").and_then(|value| value.as_str()).map(str::to_owned))
            .as_deref()
            == Some(token),
        _ => false,
    }
}

pub async fn start_sidecar(_app: &tauri::AppHandle, state: Arc<Mutex<AppState>>) -> Result<(), String> {
    let (port, health_token) = {
        let mut s = state.lock().map_err(|e| e.to_string())?;
        if s.running {
            return Err("already running".into());
        }
        s.running = true;
        (s.port, s.health_token.clone())
    };

    let parent_pid = std::process::id();
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let python = exe
        .parent()
        .ok_or_else(|| "missing app executable directory".to_string())?
        .join("python3");
    let log_dir = dirs::data_dir()
        .ok_or_else(|| "missing application support directory".to_string())?
        .join("PDF2MD")
        .join("logs");
    create_dir_all(&log_dir).map_err(|e| format!("failed to create log dir {}: {}", log_dir.display(), e))?;
    let log_path = log_dir.join("sidecar.log");
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("failed to open {}: {}", log_path.display(), e))?;
    let stderr = stdout
        .try_clone()
        .map_err(|e| format!("failed to clone {}: {}", log_path.display(), e))?;
    let spawn_result = sidecar_command(&python, port, parent_pid, &health_token)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn();
    let mut child = match spawn_result {
        Ok(child) => child,
        Err(error) => {
            state.lock().map_err(|e| e.to_string())?.running = false;
            return Err(format!("failed to start {}: {}", python.display(), error));
        }
    };

    let client = reqwest::Client::new();
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(HEALTH_STARTUP_GRACE_SECS);
    loop {
        if child_exited(&mut child).map_err(|e| e.to_string())? {
            state.lock().map_err(|e| e.to_string())?.running = false;
            return Err("sidecar exited before becoming healthy".into());
        }
        if instance_is_healthy(&client, port, &health_token).await {
            break;
        }
        if tokio::time::Instant::now() >= deadline {
            let _ = terminate_child(&mut child);
            state.lock().map_err(|e| e.to_string())?.running = false;
            return Err("sidecar did not become healthy before timeout".into());
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }

    let mut s = state.lock().map_err(|e| e.to_string())?;
    s.sidecar_child = Some(child);
    s.logs.push(format!("[sidecar] started on port {}, log {}", port, log_path.display()));

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{child_exited, reserve_loopback_port, sidecar_command, terminate_child};
    use std::ffi::OsStr;
    use std::path::Path;

    #[test]
    fn sidecar_uses_fixed_shell_and_loopback_host() {
        let command = sidecar_command(Path::new("/bundle/python3"), 8000, 42, "instance-token");
        assert_eq!(command.get_program(), OsStr::new("/bin/bash"));
        let args: Vec<_> = command.get_args().collect();
        assert_eq!(
            args,
            [
                "/bundle/python3",
                "--port",
                "8000",
                "--parent-pid",
                "42",
                "--host",
                "127.0.0.1",
                "--health-token",
                "instance-token",
            ]
        );
    }

    #[test]
    fn reserves_an_available_loopback_port() {
        let (first_listener, first_port) = reserve_loopback_port().expect("reserve first port");
        let (second_listener, second_port) = reserve_loopback_port().expect("reserve second port");
        assert_eq!(first_listener.local_addr().unwrap().port(), first_port);
        assert_eq!(second_listener.local_addr().unwrap().port(), second_port);
        assert_ne!(first_port, second_port);
    }

    #[test]
    fn detects_and_reaps_an_early_child_exit() {
        let mut child = std::process::Command::new("/usr/bin/false").spawn().unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while std::time::Instant::now() < deadline {
            if child_exited(&mut child).unwrap() {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        panic!("child did not exit");
    }

    #[test]
    fn terminates_and_reaps_a_running_child() {
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        terminate_child(&mut child).expect("terminate child");
        assert!(child.try_wait().unwrap().is_some());
    }
}

pub fn stop_sidecar(state: Arc<Mutex<AppState>>) -> Result<(), String> {
    let mut s = state.lock().map_err(|e| e.to_string())?;
    if let Some(mut child) = s.sidecar_child.take() {
        terminate_child(&mut child).map_err(|e| e.to_string())?;
        s.logs.push("[sidecar] stopped".into());
    }
    s.running = false;
    Ok(())
}

pub async fn health_loop(app: tauri::AppHandle, state: Arc<Mutex<AppState>>) {
    tokio::time::sleep(std::time::Duration::from_secs(HEALTH_STARTUP_GRACE_SECS)).await;
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(HEALTH_INTERVAL_SECS)).await;
        let (port, token) = {
            let s = state.lock().unwrap();
            (s.port, s.health_token.clone())
        };
        match instance_is_healthy(&reqwest::Client::new(), port, &token).await {
            true => {
                state.lock().unwrap().health_failures = 0;
            }
            false => {
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
                    let _ = stop_sidecar(state.clone());
                    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                    let _ = start_sidecar(&app, state.clone()).await;
                    tokio::time::sleep(std::time::Duration::from_secs(
                        HEALTH_STARTUP_GRACE_SECS,
                    ))
                    .await;
                }
            }
        }
    }
}
