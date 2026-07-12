use crate::state::AppState;
use std::fs::{create_dir_all, OpenOptions};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

const HEALTH_INTERVAL_SECS: u64 = 3;
const HEALTH_STARTUP_GRACE_SECS: u64 = 60;
const MAX_HEALTH_FAILURES: u8 = 3;

fn sidecar_command(script: &Path, port: u16, parent_pid: u32) -> Command {
    let mut command = Command::new("/bin/bash");
    command.arg(script).args([
        "--port",
        &port.to_string(),
        "--parent-pid",
        &parent_pid.to_string(),
        "--host",
        "127.0.0.1",
    ]);
    command
}

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
    let child = sidecar_command(&python, port, parent_pid)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()
        .map_err(|e| format!("failed to start {}: {}", python.display(), e))?;

    {
        let mut s = state.lock().map_err(|e| e.to_string())?;
        s.sidecar_child = Some(child);
        s.logs.push(format!("[sidecar] started on port {}, log {}", port, log_path.display()));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::sidecar_command;
    use std::ffi::OsStr;
    use std::path::Path;

    #[test]
    fn sidecar_uses_fixed_shell_and_loopback_host() {
        let command = sidecar_command(Path::new("/bundle/python3"), 8000, 42);
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
            ]
        );
    }
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
    tokio::time::sleep(std::time::Duration::from_secs(HEALTH_STARTUP_GRACE_SECS)).await;
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
                    tokio::time::sleep(std::time::Duration::from_secs(
                        HEALTH_STARTUP_GRACE_SECS,
                    ))
                    .await;
                }
            }
        }
    }
}
