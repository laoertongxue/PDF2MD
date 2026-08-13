use super::{
    build_health_client, child_exited, copy_redacted, health_failure_requires_restart,
    instance_is_healthy, make_socket_inheritable, open_rotating_log, parse_ready_line,
    prepare_retry, read_ready_line, reserve_loopback_port, reserve_loopback_port_excluding,
    restart_sidecar_core, sidecar_command, start_sidecar_core, stop_sidecar, terminate_child,
    SidecarRuntime, SidecarStartError, StartupGuard, StopReason, MAX_HEALTH_RESPONSE_BYTES,
    MAX_READY_LINE_BYTES,
};
use crate::state::{ready_api_config, AppState};
use std::collections::VecDeque;
use std::ffi::OsStr;
use std::fs::{self, OpenOptions};
use std::io::{BufReader, Read, Write};
use std::net::TcpListener;
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::sync::{Arc, Mutex};

struct ChunkReader {
    chunks: VecDeque<Vec<u8>>,
}

impl ChunkReader {
    fn new(chunks: Vec<Vec<u8>>) -> Self {
        Self {
            chunks: chunks.into(),
        }
    }
}

impl Read for ChunkReader {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        let Some(mut chunk) = self.chunks.pop_front() else {
            return Ok(0);
        };
        let length = chunk.len().min(buffer.len());
        buffer[..length].copy_from_slice(&chunk[..length]);
        if length < chunk.len() {
            self.chunks.push_front(chunk.split_off(length));
        }
        Ok(length)
    }
}

struct FixtureDirectory(PathBuf);

impl Drop for FixtureDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct SidecarCleanup(Arc<Mutex<AppState>>);

impl Drop for SidecarCleanup {
    fn drop(&mut self) {
        let _ = stop_sidecar(self.0.clone(), StopReason::Exit);
    }
}

fn write_lifecycle_fixture(control: Option<&Path>) -> (FixtureDirectory, SidecarRuntime) {
    let root = std::env::temp_dir().join(format!(
        "pdf2md-sidecar-fixture-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir_all(&root).unwrap();
    let python_path = root.join("fixture.py");
    let wrapper_path = root.join("python3");
    let log_path = root.join("sidecar.log");
    fs::write(
            &python_path,
            r#"import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--parent-pid", type=int, required=True)
parser.add_argument("--socket-fd", type=int, required=True)
args = parser.parse_args()
token = os.environ["PDF2MD_SESSION_TOKEN"]
listener = socket.socket(fileno=args.socket_fd)
host, port = listener.getsockname()
control = os.environ.get("PDF2MD_FIXTURE_CONTROL")
if control:
    import fcntl
    control_path = Path(control)
    control_path.mkdir(parents=True, exist_ok=True)
    counter_path = control_path / "counter"
    with counter_path.open("a+") as counter:
        fcntl.flock(counter, fcntl.LOCK_EX)
        counter.seek(0)
        sequence = int(counter.read() or "0") + 1
        counter.seek(0)
        counter.truncate()
        counter.write(str(sequence))
        counter.flush()
    (control_path / f"started-{sequence}").write_text(str(os.getpid()))
    gate = control_path / f"gate-{sequence}"
    while not gate.exists():
        time.sleep(0.01)
print(json.dumps({"schema": "pdf2md.sidecar.ready.v1", "host": host, "port": port}, separators=(",", ":")), flush=True)
print("fixture stdout context " + token, flush=True)
print("fixture stderr context " + token, file=sys.stderr, flush=True)

if os.environ.get("PDF2MD_FIXTURE_HANG_HEALTH") == "1":
    connection, _ = listener.accept()
    with connection:
        time.sleep(30)

while True:
    connection, _ = listener.accept()
    with connection:
        request = b""
        while b"\r\n\r\n" not in request and len(request) < 16384:
            part = connection.recv(4096)
            if not part:
                break
            request += part
        headers = {}
        for line in request.decode("latin-1").split("\r\n")[1:]:
            if not line:
                break
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        authorized = headers.get("x-pdf2md-session") == token
        status = "200 OK" if authorized else "401 Unauthorized"
        body = b'{"status":"ok"}' if authorized else b'{"detail":{"code":"session_required"}}'
        response = (
            f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + body
        )
        connection.sendall(response)
"#,
        )
        .unwrap();
    fs::write(
        &wrapper_path,
        format!(
            "#!/bin/bash\nset -euo pipefail\nexec python3 \"{}\" \"$@\"\n",
            python_path.display()
        ),
    )
    .unwrap();
    let extra_env = control
        .map(|path| {
            vec![(
                "PDF2MD_FIXTURE_CONTROL".to_string(),
                path.to_string_lossy().into_owned(),
            )]
        })
        .unwrap_or_default();
    (
        FixtureDirectory(root),
        SidecarRuntime {
            script: wrapper_path,
            log_path,
            extra_env,
            startup_timeout: std::time::Duration::from_secs(5),
        },
    )
}

async fn wait_for_path(path: &Path) {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while !path.exists() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "missing {}",
            path.display()
        );
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
}

fn process_exists(pid: i32) -> bool {
    let result = unsafe { libc::kill(pid, 0) };
    result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

#[test]
fn sidecar_uses_fixed_shell_and_passes_secret_only_in_environment() {
    let command = sidecar_command(Path::new("/bundle/python3"), 9, 42, "session-token");
    assert_eq!(command.get_program(), OsStr::new("/bin/bash"));
    let args: Vec<_> = command.get_args().collect();
    assert_eq!(
        args,
        ["/bundle/python3", "--parent-pid", "42", "--socket-fd", "9"]
    );
    assert!(args
        .iter()
        .all(|value| *value != OsStr::new("session-token")));
    assert!(command
        .get_envs()
        .any(|(key, value)| key == OsStr::new("PDF2MD_SESSION_TOKEN")
            && value == Some(OsStr::new("session-token"))));
}

#[test]
fn redacts_stdout_and_stderr_across_reads_in_a_real_log_file() {
    let secret = "session-token-crossing-read-boundaries-0123456789";
    let ready = b"{\"schema\":\"pdf2md.sidecar.ready.v1\",\"host\":\"127.0.0.1\",\"port\":43127}\n";
    let log_path = std::env::temp_dir().join(format!(
        "pdf2md-redacted-log-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let mut stdout_log = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&log_path)
        .unwrap();
    let mut stderr_log = stdout_log.try_clone().unwrap();
    let mut first_stdout_chunk = ready.to_vec();
    first_stdout_chunk.extend_from_slice(b"stdout before ");
    first_stdout_chunk.extend_from_slice(&secret.as_bytes()[..19]);
    let mut stdout = BufReader::new(ChunkReader::new(vec![
        first_stdout_chunk,
        [secret.as_bytes()[19..].to_vec(), b" after\n".to_vec()].concat(),
    ]));
    let mut stderr = ChunkReader::new(vec![
        [b"stderr before ".to_vec(), secret.as_bytes()[..11].to_vec()].concat(),
        secret.as_bytes()[11..37].to_vec(),
        [secret.as_bytes()[37..].to_vec(), b" after".to_vec()].concat(),
    ]);

    let ready_line = read_ready_line(&mut stdout).unwrap();
    assert_eq!(parse_ready_line(&ready_line, 43127).unwrap().port, 43127);
    copy_redacted(&mut stdout, &mut stdout_log, secret.as_bytes()).unwrap();
    copy_redacted(&mut stderr, &mut stderr_log, secret.as_bytes()).unwrap();
    drop(stdout_log);
    drop(stderr_log);

    let logged = fs::read_to_string(&log_path).unwrap();
    assert!(!logged.contains(secret));
    assert!(!logged.contains("pdf2md.sidecar.ready.v1"));
    assert!(logged.contains("stdout before [REDACTED] after"));
    assert!(logged.contains("stderr before [REDACTED] after"));
    fs::remove_file(log_path).unwrap();
}

#[tokio::test]
async fn rust_startup_core_rotates_live_authenticated_sidecars_and_reaps_children() {
    let (_fixture, runtime) = write_lifecycle_fixture(None);
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());

    tokio::time::timeout(
        std::time::Duration::from_secs(15),
        start_sidecar_core(state.clone(), runtime.clone(), true),
    )
    .await
    .expect("first startup timeout")
    .expect("first startup");
    let first = {
        let state = state.lock().unwrap();
        assert!(state.running);
        ready_api_config(&state).unwrap()
    };
    let first_port = reqwest::Url::parse(&first.api_base)
        .unwrap()
        .port()
        .unwrap();
    assert!(instance_is_healthy(&reqwest::Client::new(), first_port, &first.session_token).await);

    tokio::time::timeout(
        std::time::Duration::from_secs(15),
        restart_sidecar_core(state.clone(), runtime.clone()),
    )
    .await
    .expect("restart timeout")
    .expect("restart");
    let second = {
        let state = state.lock().unwrap();
        assert!(state.running);
        ready_api_config(&state).unwrap()
    };
    let second_port = reqwest::Url::parse(&second.api_base)
        .unwrap()
        .port()
        .unwrap();

    assert_ne!(first_port, second_port);
    assert_ne!(first.session_token, second.session_token);
    assert!(!instance_is_healthy(&reqwest::Client::new(), second_port, &first.session_token).await);
    assert!(instance_is_healthy(&reqwest::Client::new(), second_port, &second.session_token).await);
    assert!(!instance_is_healthy(&reqwest::Client::new(), first_port, &first.session_token).await);

    stop_sidecar(state.clone(), StopReason::Exit).unwrap();
    let state = state.lock().unwrap();
    assert!(state.sidecar_child.is_none());
    assert!(state.sidecar_log_threads.is_empty());
    let logged = fs::read_to_string(&runtime.log_path).unwrap();
    assert!(!logged.contains(&first.session_token));
    assert!(!logged.contains(&second.session_token));
    assert!(logged.contains("fixture stdout context [REDACTED]"));
    assert!(logged.contains("fixture stderr context [REDACTED]"));
}

#[tokio::test]
async fn concurrent_start_does_not_clear_the_owner_session_or_spawn_an_extra_child() {
    let control = std::env::temp_dir().join(format!(
        "pdf2md-concurrent-start-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let (_fixture, runtime) = write_lifecycle_fixture(Some(&control));
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    let first = tokio::spawn(start_sidecar_core(state.clone(), runtime.clone(), true));
    wait_for_path(&control.join("started-1")).await;
    let owner_session = state.lock().unwrap().session_token.clone();

    let duplicate = start_sidecar_core(state.clone(), runtime.clone(), true).await;

    assert!(matches!(duplicate, Err(SidecarStartError::AlreadyActive)));
    {
        let state = state.lock().unwrap();
        assert_eq!(state.session_token, owner_session);
        assert!(state.starting);
        assert!(state.error.is_none());
    }
    fs::write(control.join("gate-1"), b"continue").unwrap();
    first.await.unwrap().unwrap();
    assert!(!control.join("started-2").exists());
}

#[tokio::test]
async fn stop_during_start_invalidates_generation_and_reaps_the_old_child() {
    let control = std::env::temp_dir().join(format!(
        "pdf2md-start-stop-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let (_fixture, runtime) = write_lifecycle_fixture(Some(&control));
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    let startup = tokio::spawn(start_sidecar_core(state.clone(), runtime, true));
    wait_for_path(&control.join("started-1")).await;
    let old_pid: i32 = fs::read_to_string(control.join("started-1"))
        .unwrap()
        .parse()
        .unwrap();

    stop_sidecar(state.clone(), StopReason::Manual).unwrap();
    let result = tokio::time::timeout(std::time::Duration::from_secs(5), startup)
        .await
        .expect("stale startup did not exit")
        .unwrap();

    assert!(matches!(result, Err(SidecarStartError::Superseded)));
    assert!(!process_exists(old_pid));
    let state = state.lock().unwrap();
    assert!(!state.starting);
    assert!(!state.running);
    assert!(state.manual_stopped);
    assert!(state.session_token.is_empty());
    assert!(state.sidecar_child.is_none());
}

#[tokio::test]
async fn retry_during_start_supersedes_old_child_and_only_new_generation_commits() {
    let control = std::env::temp_dir().join(format!(
        "pdf2md-start-retry-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let (_fixture, runtime) = write_lifecycle_fixture(Some(&control));
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    let first = tokio::spawn(start_sidecar_core(state.clone(), runtime.clone(), true));
    wait_for_path(&control.join("started-1")).await;
    let old_pid: i32 = fs::read_to_string(control.join("started-1"))
        .unwrap()
        .parse()
        .unwrap();
    let old_port = state.lock().unwrap().port;
    let retry = tokio::spawn(restart_sidecar_core(state.clone(), runtime));
    wait_for_path(&control.join("started-2")).await;
    fs::write(control.join("gate-2"), b"continue").unwrap();

    assert!(matches!(
        first.await.unwrap(),
        Err(SidecarStartError::Superseded)
    ));
    retry.await.unwrap().unwrap();
    assert!(!process_exists(old_pid));
    let state = state.lock().unwrap();
    assert!(state.running);
    assert!(!state.starting);
    assert_ne!(state.port, old_port);
    assert!(state.sidecar_child.is_some());
    assert_eq!(state.sidecar_log_threads.len(), 2);
    assert!(ready_api_config(&state).is_ok());
}

#[test]
fn ready_message_has_a_strict_loopback_schema() {
    let ready = parse_ready_line(
        r#"{"schema":"pdf2md.sidecar.ready.v1","host":"127.0.0.1","port":43127}"#,
        43127,
    )
    .unwrap();

    assert_eq!(ready.port, 43127);
    assert!(parse_ready_line(
        r#"{"schema":"pdf2md.sidecar.ready.v1","host":"0.0.0.0","port":43127}"#,
        43127,
    )
    .is_err());
    assert!(parse_ready_line(
        r#"{"schema":"pdf2md.sidecar.ready.v1","host":"127.0.0.1","port":43127,"token":"leak"}"#,
        43127,
    )
    .is_err());
    assert!(parse_ready_line(
        r#"{"schema":"pdf2md.sidecar.ready.v1","host":"127.0.0.1","port":43128}"#,
        43127,
    )
    .is_err());
}

#[test]
fn ready_message_is_rejected_before_an_unbounded_first_line_can_accumulate() {
    let oversized = vec![b'x'; MAX_READY_LINE_BYTES + 1];
    let mut reader = BufReader::new(oversized.as_slice());

    let error = read_ready_line(&mut reader).unwrap_err();

    assert_eq!(error, "invalid sidecar ready message");
}

#[test]
fn rotating_log_enforces_a_hard_current_and_backup_size_limit() {
    let log_path = std::env::temp_dir().join(format!(
        "pdf2md-rotating-log-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let backup_path = PathBuf::from(format!("{}.1", log_path.display()));
    let mut log = open_rotating_log(&log_path, 64).unwrap();

    log.write_all(&[b'a'; 150]).unwrap();
    log.flush().unwrap();
    drop(log);

    assert!(fs::metadata(&log_path).unwrap().len() <= 64);
    assert!(fs::metadata(&backup_path).unwrap().len() <= 64);
    fs::remove_file(log_path).unwrap();
    fs::remove_file(backup_path).unwrap();
}

fn spawn_health_server(
    body: Vec<u8>,
    captured: Arc<Mutex<Vec<u8>>>,
) -> (u16, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let thread = std::thread::spawn(move || {
        let (mut connection, _) = listener.accept().unwrap();
        let mut request = [0_u8; 8192];
        let read = connection.read(&mut request).unwrap();
        captured.lock().unwrap().extend_from_slice(&request[..read]);
        let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
        connection.write_all(headers.as_bytes()).unwrap();
        connection.write_all(&body).unwrap();
    });
    (port, thread)
}

#[tokio::test]
async fn health_client_never_sends_loopback_session_through_environment_proxy() {
    static PROXY_ENV_LOCK: Mutex<()> = Mutex::new(());
    let _environment_guard = PROXY_ENV_LOCK.lock().unwrap();
    let proxy_listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    proxy_listener.set_nonblocking(true).unwrap();
    let proxy_port = proxy_listener.local_addr().unwrap().port();
    let proxy_capture = Arc::new(Mutex::new(Vec::new()));
    let proxy_capture_thread = proxy_capture.clone();
    let proxy_done = Arc::new(AtomicBool::new(false));
    let proxy_done_thread = proxy_done.clone();
    let proxy_thread = std::thread::spawn(move || {
        while !proxy_done_thread.load(Ordering::SeqCst) {
            match proxy_listener.accept() {
                Ok((mut connection, _)) => {
                    let mut request = [0_u8; 8192];
                    if let Ok(read) = connection.read(&mut request) {
                        proxy_capture_thread
                            .lock()
                            .unwrap()
                            .extend_from_slice(&request[..read]);
                    }
                    break;
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                Err(_) => break,
            }
        }
    });
    let target_capture = Arc::new(Mutex::new(Vec::new()));
    let (target_port, target_thread) =
        spawn_health_server(b"{\"status\":\"ok\"}".to_vec(), target_capture.clone());
    let previous_proxy = std::env::var_os("HTTP_PROXY");
    std::env::set_var("HTTP_PROXY", format!("http://127.0.0.1:{proxy_port}"));
    let secret = "proxy-isolation-session-token-0123456789abcdef";
    let client = build_health_client().unwrap();

    match previous_proxy {
        Some(value) => std::env::set_var("HTTP_PROXY", value),
        None => std::env::remove_var("HTTP_PROXY"),
    }
    drop(_environment_guard);

    let healthy = instance_is_healthy(&client, target_port, secret).await;
    proxy_done.store(true, Ordering::SeqCst);
    target_thread.join().unwrap();
    proxy_thread.join().unwrap();
    assert!(healthy);
    assert!(proxy_capture.lock().unwrap().is_empty());
    let target_request = String::from_utf8_lossy(&target_capture.lock().unwrap()).to_string();
    assert!(target_request.contains(secret));
}

#[tokio::test]
async fn hanging_health_peer_returns_false_within_the_request_deadline() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let (accepted_tx, accepted_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let thread = std::thread::spawn(move || {
        let (_connection, _) = listener.accept().unwrap();
        accepted_tx.send(()).unwrap();
        let _ = release_rx.recv_timeout(std::time::Duration::from_secs(3));
    });
    let started = std::time::Instant::now();
    let check = tokio::spawn(async move {
        let token = "a".repeat(64);
        instance_is_healthy(&build_health_client().unwrap(), port, &token).await
    });
    tokio::task::spawn_blocking(move || {
        accepted_rx.recv_timeout(std::time::Duration::from_secs(1))
    })
    .await
    .unwrap()
    .unwrap();

    let healthy = tokio::time::timeout(std::time::Duration::from_secs(2), check)
        .await
        .expect("health request exceeded total timeout")
        .unwrap();

    release_tx.send(()).unwrap();
    thread.join().unwrap();
    assert!(!healthy);
    assert!(started.elapsed() < std::time::Duration::from_secs(2));
}

#[tokio::test]
async fn startup_deadline_expires_when_fixture_accepts_health_without_responding() {
    let (_fixture, mut runtime) = write_lifecycle_fixture(None);
    runtime
        .extra_env
        .push(("PDF2MD_FIXTURE_HANG_HEALTH".to_string(), "1".to_string()));
    runtime.startup_timeout = std::time::Duration::from_millis(1200);
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    let started = std::time::Instant::now();

    let result = tokio::time::timeout(
        std::time::Duration::from_secs(3),
        start_sidecar_core(state.clone(), runtime, true),
    )
    .await
    .expect("startup ignored its deadline");

    assert!(matches!(
        result,
        Err(SidecarStartError::Failed { message, .. })
            if message == "sidecar did not become healthy before timeout"
    ));
    assert!(started.elapsed() < std::time::Duration::from_secs(3));
    let state = state.lock().unwrap();
    assert!(!state.running);
    assert!(state.sidecar_child.is_none());
}

#[tokio::test]
async fn health_response_larger_than_the_fixed_limit_is_rejected() {
    let captured = Arc::new(Mutex::new(Vec::new()));
    let body = vec![b'x'; MAX_HEALTH_RESPONSE_BYTES + 1];
    let (port, thread) = spawn_health_server(body, captured);

    let healthy = instance_is_healthy(&build_health_client().unwrap(), port, &"b".repeat(64)).await;

    thread.join().unwrap();
    assert!(!healthy);
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
fn consecutive_startup_material_rotates_port_and_session() {
    let (first_listener, first_port) = reserve_loopback_port().expect("reserve first port");
    let first_session = crate::state::generate_session_token();
    drop(first_listener);
    let (second_listener, second_port) =
        reserve_loopback_port_excluding(first_port).expect("reserve a different second port");
    let second_session = crate::state::generate_session_token();

    assert_eq!(
        second_listener.local_addr().unwrap().ip().to_string(),
        "127.0.0.1"
    );
    assert_ne!(first_port, second_port);
    assert_ne!(first_session, second_session);
}

#[test]
fn reserved_socket_cannot_be_stolen_before_sidecar_inherits_it() {
    let (listener, port) = reserve_loopback_port().expect("reserve port");
    make_socket_inheritable(&listener).expect("make socket inheritable");

    let competitor = TcpListener::bind(("127.0.0.1", port));

    assert_eq!(
        competitor.unwrap_err().kind(),
        std::io::ErrorKind::AddrInUse
    );
    let flags = unsafe { libc::fcntl(listener.as_raw_fd(), libc::F_GETFD) };
    assert_eq!(flags & libc::FD_CLOEXEC, 0);
}

#[test]
fn startup_guard_rolls_back_running_state_after_failure() {
    let state = Arc::new(Mutex::new(AppState {
        generation: 7,
        starting: true,
        running: false,
        ..Default::default()
    }));

    drop(StartupGuard::new(state.clone(), 7));

    let state = state.lock().unwrap();
    assert!(!state.starting);
    assert!(!state.running);
}

#[test]
fn records_a_structured_failure_for_status_consumers() {
    let secret = "session-token-that-must-never-appear-in-logs";
    let state = Arc::new(Mutex::new(AppState {
        session_token: secret.into(),
        ..Default::default()
    }));

    super::record_failure(&state, 0, "startup", format!("runtime missing {secret}"));

    let state = state.lock().unwrap();
    assert_eq!(state.service_state, "failed");
    let error = state.error.as_ref().expect("structured error");
    assert_eq!(error.category, "startup");
    assert_eq!(error.message, "runtime missing [REDACTED]");
    assert!(!state.logs.join("\n").contains(secret));
    assert!(state.session_token.is_empty());
}

#[test]
fn stale_failure_cannot_clear_the_current_generation() {
    let secret = "current-session-secret";
    let state = Arc::new(Mutex::new(AppState {
        generation: 9,
        session_token: secret.into(),
        running: true,
        service_state: "running".into(),
        ..Default::default()
    }));

    super::record_failure(
        &state,
        8,
        "configuration",
        "stale health client failure".into(),
    );

    let state = state.lock().unwrap();
    assert!(state.running);
    assert_eq!(state.service_state, "running");
    assert_eq!(state.session_token, secret);
    assert!(state.error.is_none());
}

#[test]
fn classifies_actionable_startup_failures() {
    assert_eq!(
        super::classify_startup_error("failed to start python3"),
        "spawn"
    );
    assert_eq!(
        super::classify_startup_error("sidecar exited before becoming healthy"),
        "early_exit"
    );
    assert_eq!(
        super::classify_startup_error("sidecar did not become healthy before timeout"),
        "health_timeout"
    );
}

#[test]
fn detects_and_reaps_an_early_child_exit() {
    let mut child = std::process::Command::new("/usr/bin/false")
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while std::time::Instant::now() < deadline {
        if child_exited(&mut child).unwrap() {
            child.wait().unwrap();
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let _ = child.kill();
    let _ = child.wait();
    panic!("child did not exit");
}

#[test]
fn terminates_and_reaps_a_running_child() {
    let marker = std::env::temp_dir().join(format!("pdf2md-term-{}", std::process::id()));
    let script = format!(
        "trap 'touch {} ; exit 0' TERM; while :; do sleep 1; done",
        marker.display()
    );
    let mut child = std::process::Command::new("/bin/sh")
        .args(["-c", &script])
        .spawn()
        .unwrap();
    std::thread::sleep(std::time::Duration::from_millis(100));
    terminate_child(&mut child).expect("terminate child");
    assert!(child.try_wait().unwrap().is_some());
    assert!(
        marker.exists(),
        "child must receive SIGTERM before any SIGKILL fallback"
    );
    let _ = std::fs::remove_file(marker);
}

#[test]
fn manual_stop_survives_multiple_unhealthy_cycles() {
    let state = Arc::new(Mutex::new(AppState {
        desired_running: true,
        running: true,
        ..Default::default()
    }));

    stop_sidecar(state.clone(), StopReason::Manual).unwrap();

    let mut state = state.lock().unwrap();
    for _ in 0..(super::MAX_HEALTH_FAILURES * 2) {
        assert!(!health_failure_requires_restart(&mut state));
    }

    assert_eq!(state.health_failures, 0);
    assert!(state.manual_stopped);
}

#[test]
fn application_exit_is_not_reported_as_manual_or_health_failure() {
    let state = Arc::new(Mutex::new(AppState {
        desired_running: true,
        running: true,
        ..Default::default()
    }));

    stop_sidecar(state.clone(), StopReason::Exit).unwrap();

    let state = state.lock().unwrap();
    assert!(!state.desired_running);
    assert!(!state.manual_stopped);
    assert!(state.error.is_none());
    assert_eq!(state.health_failures, 0);
}

#[test]
fn retry_restores_desired_running_state() {
    let mut state = AppState {
        desired_running: false,
        manual_stopped: true,
        ..Default::default()
    };

    prepare_retry(&mut state);

    assert!(state.desired_running);
    assert!(!state.manual_stopped);
    assert_eq!(state.service_state, "restarting");
}

#[test]
fn real_health_failure_still_requests_automatic_restart() {
    let mut state = AppState {
        desired_running: true,
        running: true,
        ..Default::default()
    };

    for _ in 1..super::MAX_HEALTH_FAILURES {
        assert!(!health_failure_requires_restart(&mut state));
    }
    assert!(health_failure_requires_restart(&mut state));
    assert_eq!(state.service_state, "restarting");
}
