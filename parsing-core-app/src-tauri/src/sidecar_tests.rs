use super::{
    build_health_client, child_exited, copy_redacted, health_failure_requires_restart,
    instance_is_healthy, make_socket_close_on_exec, open_rotating_log, parse_ready_line,
    prepare_private_log_directory, prepare_retry, read_ready_line, reserve_loopback_port,
    reserve_loopback_port_excluding, restart_sidecar_core, run_prepared_sidecar, sidecar_command,
    start_sidecar_core, stop_sidecar, stop_sidecar_async, terminate_child, SidecarAction,
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
#[cfg(unix)]
use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::sync::{Arc, Mutex};

struct ChunkReader {
    chunks: VecDeque<Vec<u8>>,
}

struct FailingLogWriter {
    message: String,
}

impl Write for FailingLogWriter {
    fn write(&mut self, _buffer: &[u8]) -> std::io::Result<usize> {
        Err(std::io::Error::other(self.message.clone()))
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
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

#[cfg(target_os = "macos")]
struct ProcessGroupCleanup(i32);

#[cfg(target_os = "macos")]
impl Drop for ProcessGroupCleanup {
    fn drop(&mut self) {
        if self.0 > 1 {
            let _ = unsafe { libc::kill(-self.0, libc::SIGKILL) };
        }
    }
}

#[cfg(target_os = "macos")]
struct ProcessCleanup(i32);

#[cfg(target_os = "macos")]
impl Drop for ProcessCleanup {
    fn drop(&mut self) {
        kill_process_for_test_cleanup(self.0);
    }
}

fn generated_launcher_template() -> String {
    let helper = Path::new(env!("CARGO_MANIFEST_DIR")).join("../scripts/sidecar_runtime.py");
    let output = std::process::Command::new("/usr/bin/python3")
        .args(["-I", "-S", "-B"])
        .arg(helper)
        .arg("emit-launcher")
        .env_clear()
        .env("HOME", "/tmp")
        .env("PATH", "/usr/bin:/bin")
        .env("TMPDIR", "/tmp")
        .output()
        .expect("run production launcher emitter");
    assert!(
        output.status.success(),
        "launcher emitter failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).expect("launcher must be UTF-8")
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
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--parent-pid", type=int, required=True)
parser.add_argument("--socket-fd", type=int, required=True)
parser.add_argument("--session-token-fd", type=int, required=True)
args = parser.parse_args()
if "PDF2MD_SESSION_TOKEN" in os.environ:
    raise RuntimeError("session token must not be inherited through the environment")
token_bytes = b""
while True:
    chunk = os.read(args.session_token_fd, 256)
    if not chunk:
        break
    token_bytes += chunk
os.close(args.session_token_fd)
token = token_bytes.decode("ascii")
listener = socket.socket(fileno=args.socket_fd)
host, port = listener.getsockname()
if os.environ.get("PDF2MD_FIXTURE_IGNORE_TERM") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
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

descendant_pid_path = os.environ.get("PDF2MD_FIXTURE_DESCENDANT_PID")
if descendant_pid_path:
    descendant = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import json,os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(os.environ['PDF2MD_FIXTURE_DESCENDANT_PID']).write_text(json.dumps({'pid': os.getpid(), 'sid': os.getsid(0), 'pgid': os.getpgrp()})); time.sleep(30)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout,
        stderr=sys.stderr,
        preexec_fn=os.setpgrp,
    )

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
        lines = request.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if not line:
                break
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        authorized = headers.get("x-pdf2md-session") == token
        should_exit = False
        if method == "POST" and path == "/shutdown" and authorized:
            record = os.environ.get("PDF2MD_FIXTURE_SHUTDOWN_RECORD")
            if record:
                with Path(record).open("a") as output:
                    output.write(json.dumps({
                        "authorized": True,
                        "pid": os.getpid(),
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    }, separators=(",", ":")) + "\n")
            mode = os.environ.get("PDF2MD_FIXTURE_SHUTDOWN_MODE", "ack")
            if mode == "hang":
                time.sleep(30)
                continue
            if mode == "reject":
                status = "503 Service Unavailable"
                body = b'{"detail":{"code":"shutdown_unavailable"}}'
            else:
                status = "200 OK"
                body = b'{"status":"shutting_down"}'
                should_exit = True
        else:
            status = "200 OK" if authorized else "401 Unauthorized"
            body = b'{"status":"ok"}' if authorized else b'{"detail":{"code":"session_required"}}'
        response = (
            f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + body
        )
        connection.sendall(response)
    if should_exit:
        break
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

async fn wait_for_nonempty_file(path: &Path) {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while !fs::metadata(path).is_ok_and(|metadata| metadata.len() > 0) {
        assert!(
            tokio::time::Instant::now() < deadline,
            "missing {}",
            path.display()
        );
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
}

async fn wait_for_state(
    state: &Arc<Mutex<AppState>>,
    predicate: impl Fn(&AppState) -> bool,
) -> bool {
    wait_for_state_by(
        state,
        predicate,
        std::time::Instant::now() + std::time::Duration::from_secs(5),
    )
    .await
}

async fn wait_for_state_by(
    state: &Arc<Mutex<AppState>>,
    predicate: impl Fn(&AppState) -> bool,
    deadline: std::time::Instant,
) -> bool {
    loop {
        if state.lock().is_ok_and(|state| predicate(&state)) {
            return true;
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
}

#[cfg(unix)]
fn kill_process_for_test_cleanup(pid: i32) {
    if pid > 1 {
        let _ = unsafe { libc::kill(pid, libc::SIGKILL) };
    }
}

#[cfg(unix)]
fn wait_until_process_is_gone(pid: i32) -> bool {
    wait_until_process_is_gone_by(
        pid,
        std::time::Instant::now() + std::time::Duration::from_secs(3),
    )
}

#[cfg(unix)]
fn wait_until_process_is_gone_by(pid: i32, deadline: std::time::Instant) -> bool {
    while std::time::Instant::now() < deadline {
        if !process_exists(pid) {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
    false
}

fn process_exists(pid: i32) -> bool {
    let result = unsafe { libc::kill(pid, 0) };
    result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

fn poison_state(state: &Arc<Mutex<AppState>>) {
    let poison_state = state.clone();
    assert!(std::thread::spawn(move || {
        let _guard = poison_state.lock().unwrap();
        panic!("poison sidecar state for lifecycle regression");
    })
    .join()
    .is_err());
}

#[test]
fn sidecar_uses_fixed_shell_and_passes_secret_only_through_an_inherited_fd() {
    let command = sidecar_command(Path::new("/bundle/python3"), 9, 11, 42);
    assert_eq!(command.get_program(), OsStr::new("/bin/bash"));
    let args: Vec<_> = command.get_args().collect();
    assert_eq!(
        args,
        [
            "/bundle/python3",
            "--parent-pid",
            "42",
            "--socket-fd",
            "3",
            "--session-token-fd",
            "4"
        ]
    );
    let removed_session = command
        .get_envs()
        .find(|(key, _)| *key == OsStr::new("PDF2MD_SESSION_TOKEN"))
        .expect("the parent session-token environment must be explicitly removed");
    assert_eq!(removed_session.1, None);
}

#[test]
fn session_token_channel_remains_close_on_exec_in_the_parent() {
    let mut reader = super::session_token_channel("parent-only-session-token").unwrap();
    let flags = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_GETFD) };
    let mut token = String::new();
    reader.read_to_string(&mut token).unwrap();

    assert_ne!(flags & libc::FD_CLOEXEC, 0);
    assert_eq!(token, "parent-only-session-token");
}

#[cfg(target_os = "macos")]
#[test]
fn bundled_wrapper_reaps_independent_session_descendant_when_desktop_parent_dies() {
    let root = std::env::temp_dir().join(format!(
        "pdf2md-parent-watchdog-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let _fixture = FixtureDirectory(root.clone());
    let macos = root.join("MacOS");
    let runtime_bin = root.join("Resources/python-runtime/bin");
    let runtime_lib = root.join("Resources/python-runtime/lib/python3.12");
    fs::create_dir_all(&macos).unwrap();
    fs::create_dir_all(&runtime_bin).unwrap();
    fs::create_dir_all(&runtime_lib).unwrap();

    let wrapper = macos.join("python3");
    fs::write(&wrapper, generated_launcher_template()).unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();

    let sidecar_pid_path = root.join("sidecar.pid");
    let descendant_pid_path = root.join("descendant.pid");
    let fake_python = runtime_bin.join("python3");
    symlink("/usr/bin/python3", &fake_python).unwrap();
    let serving = root.join("Resources/src/parsing_core/serving");
    fs::create_dir_all(&serving).unwrap();
    fs::write(serving.parent().unwrap().join("__init__.py"), b"").unwrap();
    fs::write(serving.join("__init__.py"), b"").unwrap();
    fs::copy(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../src/parsing_core/serving/lifecycle.py"),
        serving.join("lifecycle.py"),
    )
    .unwrap();
    fs::write(
        serving.join("serve.py"),
        r#"import argparse
import json
import os
import time
from pathlib import Path

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--socket-fd", type=int, required=True)
parser.add_argument("--session-token-fd", type=int, required=True)
args, _ = parser.parse_known_args()
if "PDF2MD_SESSION_TOKEN" in os.environ:
    raise RuntimeError("session token must not be inherited through the environment")
while os.read(args.session_token_fd, 256):
    pass
os.close(args.session_token_fd)
Path(os.environ["PDF2MD_WATCHDOG_SIDECAR_PID_FILE"]).write_text(str(os.getpid()))
intermediate = os.fork()
if intermediate == 0:
    os.setsid()
    session_leader = os.getpid()
    descendant = os.fork()
    if descendant != 0:
        os._exit(0)
    os.close(args.socket_fd)
    descendant_path = Path(os.environ["PDF2MD_WATCHDOG_DESCENDANT_PID_FILE"])
    temporary_path = descendant_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps({
        "pid": os.getpid(),
        "session_leader": session_leader,
    }, separators=(",", ":")))
    os.replace(temporary_path, descendant_path)
    while True:
        time.sleep(30)
os.waitpid(intermediate, 0)
while True:
    time.sleep(1)
"#,
    )
    .unwrap();

    let wrapper_pid_path = root.join("wrapper.pid");
    let owner = root.join("owner.py");
    fs::write(
        &owner,
        r#"import os
import socket
import subprocess
import sys
import time
from pathlib import Path

wrapper = sys.argv[1]
wrapper_pid_path = Path(sys.argv[2])
sidecar_pid_path = Path(sys.argv[3])
descendant_pid_path = Path(sys.argv[4])
environment = os.environ.copy()
environment["PDF2MD_WATCHDOG_SIDECAR_PID_FILE"] = str(sidecar_pid_path)
environment["PDF2MD_WATCHDOG_DESCENDANT_PID_FILE"] = str(descendant_pid_path)
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen()
listener.set_inheritable(True)
token_read, token_write = os.pipe()
os.set_inheritable(token_read, True)
os.write(token_write, b"parent-watchdog-test-session-0123456789abcdef")
os.close(token_write)
child = subprocess.Popen(
    [
        "/bin/bash",
        wrapper,
        "--parent-pid",
        str(os.getpid()),
        "--socket-fd",
        str(listener.fileno()),
        "--session-token-fd",
        str(token_read),
    ],
    env=environment,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    pass_fds=(listener.fileno(), token_read),
    start_new_session=True,
)
listener.close()
os.close(token_read)
wrapper_pid_path.write_text(str(child.pid))
deadline = time.monotonic() + 5
while (not sidecar_pid_path.exists() or not descendant_pid_path.exists()) and time.monotonic() < deadline:
    time.sleep(0.01)
if not sidecar_pid_path.exists() or not descendant_pid_path.exists():
    child.kill()
    raise SystemExit(2)
"#,
    )
    .unwrap();

    let status = std::process::Command::new("/usr/bin/python3")
        .args([
            owner.as_os_str(),
            wrapper.as_os_str(),
            wrapper_pid_path.as_os_str(),
            sidecar_pid_path.as_os_str(),
            descendant_pid_path.as_os_str(),
        ])
        .env("HOME", root.join("home"))
        .status()
        .unwrap();
    assert!(status.success(), "parent fixture must launch the sidecar");

    let wrapper_pid = fs::read_to_string(&wrapper_pid_path)
        .unwrap()
        .parse::<i32>()
        .unwrap();
    let sidecar_pid = fs::read_to_string(&sidecar_pid_path)
        .unwrap()
        .parse::<i32>()
        .unwrap();
    let descendant: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&descendant_pid_path).unwrap()).unwrap();
    let descendant_pid = descendant["pid"].as_i64().unwrap() as i32;
    let session_leader = descendant["session_leader"].as_i64().unwrap() as i32;
    let _cleanup = ProcessGroupCleanup(wrapper_pid);
    let _descendant_cleanup = ProcessCleanup(descendant_pid);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);

    assert_eq!(unsafe { libc::getsid(descendant_pid) }, session_leader);
    assert_eq!(unsafe { libc::getpgid(descendant_pid) }, session_leader);

    assert!(
        wait_until_process_is_gone_by(wrapper_pid, deadline),
        "packaged wrapper survived after its desktop parent exited"
    );
    assert!(
        wait_until_process_is_gone_by(sidecar_pid, deadline),
        "sidecar survived after its desktop parent exited"
    );
    assert!(
        wait_until_process_is_gone_by(descendant_pid, deadline),
        "independent-session descendant survived after its desktop parent exited"
    );
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

#[test]
fn output_log_write_failure_closes_the_api_until_retry_without_exposing_the_session() {
    let secret = "log-session-secret-0123456789abcdef0123456789abcdef";
    let state = Arc::new(Mutex::new(AppState {
        generation: 12,
        session_token: secret.into(),
        running: true,
        service_state: "running".into(),
        ..Default::default()
    }));
    let mut reader = std::io::Cursor::new(b"sidecar output".to_vec());
    let mut writer = FailingLogWriter {
        message: format!("disk full while writing {secret}"),
    };

    let error = super::copy_output_to_log(
        &mut reader,
        &mut writer,
        secret.as_bytes(),
        &state,
        12,
        super::OutputStream::Stderr,
    )
    .unwrap_err();

    assert!(error.to_string().contains("disk full"));
    let state = state.lock().unwrap();
    assert!(!state.running);
    assert_eq!(state.service_state, "failed");
    let error = state.error.as_ref().expect("structured logging error");
    assert_eq!(error.category, "logging");
    assert_eq!(error.message, "sidecar stderr logging failed");
    assert!(state.session_token.is_empty());
    assert!(ready_api_config(&state).is_err());
    assert!(!state.logs.join("\n").contains(secret));
    assert!(state.logs.join("\n").contains("[REDACTED]"));
}

#[test]
fn startup_failure_recording_does_not_overwrite_a_logging_failure() {
    let session = "startup-log-session-0123456789abcdef0123456789abcdef";
    let state = Arc::new(Mutex::new(AppState {
        generation: 13,
        session_token: session.into(),
        starting: true,
        running: false,
        desired_running: true,
        service_state: "failed".into(),
        error: Some(crate::state::ServiceError {
            category: "logging".into(),
            message: "sidecar stderr logging failed".into(),
        }),
        ..Default::default()
    }));
    let startup_error = SidecarStartError::Failed {
        message: "sidecar stderr logging failed".into(),
        generation: 13,
    };

    let returned = super::record_start_failure(&state, &startup_error);
    let state = state.lock().unwrap();

    assert_eq!(returned, "sidecar stderr logging failed");
    assert_eq!(state.service_state, "failed");
    assert!(!state.running);
    assert_eq!(
        state.error.as_ref().map(|error| error.category.as_str()),
        Some("logging")
    );
    assert!(state.session_token.is_empty());
    assert!(ready_api_config(&state).is_err());
}

#[tokio::test]
async fn startup_commit_does_not_overwrite_a_logging_failure() {
    let control = std::env::temp_dir().join(format!(
        "pdf2md-startup-log-failure-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let _control_cleanup = FixtureDirectory(control.clone());
    let (_fixture, runtime) = write_lifecycle_fixture(Some(&control));
    let state = Arc::new(Mutex::new(AppState {
        desired_running: true,
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    let startup = tokio::spawn(start_sidecar_core(state.clone(), runtime, true));
    wait_for_nonempty_file(&control.join("started-1")).await;
    let (generation, session) = {
        let state = state.lock().unwrap();
        (state.generation, state.session_token.clone())
    };
    let mut reader = std::io::Cursor::new(b"sidecar output".to_vec());
    let mut writer = FailingLogWriter {
        message: "disk full during startup".into(),
    };
    super::copy_output_to_log(
        &mut reader,
        &mut writer,
        session.as_bytes(),
        &state,
        generation,
        super::OutputStream::Stderr,
    )
    .unwrap_err();
    fs::write(control.join("gate-1"), b"continue").unwrap();

    let result = tokio::time::timeout(std::time::Duration::from_secs(5), startup)
        .await
        .expect("startup did not finish after logging failure")
        .unwrap();
    let state = state.lock().unwrap();

    assert!(
        matches!(
            result,
            Err(SidecarStartError::Failed {
                ref message,
                generation: failed_generation,
            }) if message == "sidecar stderr logging failed" && failed_generation == generation
        ),
        "unexpected startup result: {result:?}"
    );
    assert_eq!(state.service_state, "failed");
    assert_eq!(
        state.error.as_ref().map(|error| error.category.as_str()),
        Some("logging")
    );
    assert!(!state.starting);
    assert!(!state.running);
    assert!(state.sidecar_child.is_none());
    assert!(state.sidecar_log_threads.is_empty());
    assert!(ready_api_config(&state).is_err());
}

#[tokio::test]
async fn logging_failure_terminates_the_owned_sidecar_and_revokes_its_session() {
    let (_fixture, runtime) = write_lifecycle_fixture(None);
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    start_sidecar_core(state.clone(), runtime, true)
        .await
        .expect("start fixture");
    let (leader_pid, generation, old_port, old_session) = {
        let state = state.lock().unwrap();
        (
            state.sidecar_child.as_ref().unwrap().id() as i32,
            state.sidecar_owner_generation.unwrap(),
            state.port,
            state.session_token.clone(),
        )
    };
    let (go_tx, go_rx) = mpsc::channel();
    let failure_state = state.clone();
    let failure_session = old_session.clone();
    let failure_thread = std::thread::spawn(move || {
        let _ = go_rx.recv();
        let mut reader = std::io::Cursor::new(b"sidecar logging failure".to_vec());
        let mut writer = FailingLogWriter {
            message: format!("disk full while writing {failure_session}"),
        };
        let _ = super::copy_output_to_log(
            &mut reader,
            &mut writer,
            failure_session.as_bytes(),
            &failure_state,
            generation,
            super::OutputStream::Stderr,
        );
    });
    state
        .lock()
        .unwrap()
        .sidecar_log_threads
        .push(failure_thread);
    go_tx.send(()).unwrap();

    let owner_released = wait_for_state(&state, |state| {
        state.sidecar_child.is_none() && state.sidecar_log_threads.is_empty()
    })
    .await;
    let process_gone = wait_until_process_is_gone(leader_pid);
    let old_api_alive = instance_is_healthy(&reqwest::Client::new(), old_port, &old_session).await;
    let snapshot = {
        let state = state.lock().unwrap();
        (
            state.session_token.clone(),
            ready_api_config(&state).is_err(),
            state.error.as_ref().map(|error| error.category.clone()),
            state.logs.join("\n"),
        )
    };
    if !owner_released {
        let _ = stop_sidecar(state.clone(), StopReason::Exit);
    }

    assert!(owner_released);
    assert!(process_gone);
    assert!(!old_api_alive);
    assert!(snapshot.0.is_empty());
    assert!(snapshot.1);
    assert_eq!(snapshot.2.as_deref(), Some("logging"));
    assert!(!snapshot.3.contains(&old_session));
}

#[tokio::test]
async fn rust_startup_core_gracefully_rotates_authenticated_sessions_and_reaps_children() {
    let (_fixture, mut runtime) = write_lifecycle_fixture(None);
    let shutdown_record = runtime.log_path.with_extension("shutdown.jsonl");
    runtime.extra_env.push((
        "PDF2MD_FIXTURE_SHUTDOWN_RECORD".into(),
        shutdown_record.to_string_lossy().into_owned(),
    ));
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
    let (first, first_pid) = {
        let state = state.lock().unwrap();
        assert!(state.running);
        (
            ready_api_config(&state).unwrap(),
            state.sidecar_child.as_ref().unwrap().id(),
        )
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
    let first_shutdown: serde_json::Value = serde_json::from_str(
        fs::read_to_string(&shutdown_record)
            .unwrap()
            .lines()
            .next()
            .unwrap(),
    )
    .unwrap();
    let (second, second_pid) = {
        let state = state.lock().unwrap();
        assert!(state.running);
        (
            ready_api_config(&state).unwrap(),
            state.sidecar_child.as_ref().unwrap().id(),
        )
    };
    let second_port = reqwest::Url::parse(&second.api_base)
        .unwrap()
        .port()
        .unwrap();

    assert_ne!(first_port, second_port);
    assert_ne!(first.session_token, second.session_token);
    assert_ne!(first_pid, second_pid);
    assert_eq!(first_shutdown["authorized"], true);
    assert_eq!(first_shutdown["pid"], first_pid);
    assert!(!instance_is_healthy(&reqwest::Client::new(), second_port, &first.session_token).await);
    assert!(instance_is_healthy(&reqwest::Client::new(), second_port, &second.session_token).await);
    assert!(!instance_is_healthy(&reqwest::Client::new(), first_port, &first.session_token).await);

    stop_sidecar(state.clone(), StopReason::Exit).unwrap();
    let shutdowns = fs::read_to_string(&shutdown_record).unwrap();
    let shutdowns = shutdowns
        .lines()
        .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(shutdowns.len(), 2);
    assert_eq!(shutdowns[1]["authorized"], true);
    assert_eq!(shutdowns[1]["pid"], second_pid);
    let state = state.lock().unwrap();
    assert!(state.sidecar_child.is_none());
    assert!(state.sidecar_log_threads.is_empty());
    let logged = fs::read_to_string(&runtime.log_path).unwrap();
    assert!(!logged.contains(&first.session_token));
    assert!(!logged.contains(&second.session_token));
    assert!(logged.contains("fixture stdout context [REDACTED]"));
    assert!(logged.contains("fixture stderr context [REDACTED]"));
}

#[cfg(unix)]
#[tokio::test]
async fn graceful_shutdown_timeout_falls_back_to_signals_within_a_bound() {
    let (_fixture, mut runtime) = write_lifecycle_fixture(None);
    let shutdown_record = runtime.log_path.with_extension("shutdown-timeout.jsonl");
    runtime.extra_env.extend([
        (
            "PDF2MD_FIXTURE_SHUTDOWN_RECORD".into(),
            shutdown_record.to_string_lossy().into_owned(),
        ),
        ("PDF2MD_FIXTURE_SHUTDOWN_MODE".into(), "hang".into()),
        ("PDF2MD_FIXTURE_IGNORE_TERM".into(), "1".into()),
    ]);
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    start_sidecar_core(state.clone(), runtime, true)
        .await
        .expect("start timeout fixture");
    let leader_pid = state.lock().unwrap().sidecar_child.as_ref().unwrap().id() as i32;

    let started = std::time::Instant::now();
    let result = stop_sidecar(state, StopReason::Exit);
    let elapsed = started.elapsed();

    assert!(result.is_ok());
    assert!(elapsed < std::time::Duration::from_secs(7));
    assert!(wait_until_process_is_gone(leader_pid));
    let record = fs::read_to_string(shutdown_record).unwrap();
    let request: serde_json::Value = serde_json::from_str(record.trim()).unwrap();
    assert_eq!(request["authorized"], true);
    assert_eq!(request["pid"], leader_pid);
}

#[cfg(unix)]
#[tokio::test]
async fn stop_terminates_independent_process_group_descendant_in_sidecar_session() {
    let (_fixture, mut runtime) = write_lifecycle_fixture(None);
    let descendant_pid_path = runtime.log_path.with_extension("descendant.pid");
    runtime.extra_env.push((
        "PDF2MD_FIXTURE_DESCENDANT_PID".into(),
        descendant_pid_path.to_string_lossy().into_owned(),
    ));
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());

    start_sidecar_core(state.clone(), runtime, true)
        .await
        .expect("start fixture with descendant");
    wait_for_nonempty_file(&descendant_pid_path).await;
    let descendant: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&descendant_pid_path).unwrap()).unwrap();
    let descendant_pid = descendant["pid"].as_i64().unwrap() as i32;
    let descendant_sid = descendant["sid"].as_i64().unwrap() as i32;
    let descendant_process_group = descendant["pgid"].as_i64().unwrap() as i32;
    let leader_pid = state
        .lock()
        .unwrap()
        .sidecar_child
        .as_ref()
        .expect("sidecar leader")
        .id() as i32;
    let process_group_id = state
        .lock()
        .unwrap()
        .sidecar_process_group
        .expect("isolated sidecar process group");
    let leader_sid = unsafe { libc::getsid(leader_pid) };
    let app_sid = unsafe { libc::getsid(0) };
    let isolated_session = leader_sid == leader_pid && leader_sid != app_sid;
    let descendant_boundary = descendant_sid == leader_sid
        && descendant_process_group == descendant_pid
        && descendant_process_group != process_group_id;
    assert!(process_exists(leader_pid));
    assert!(process_exists(descendant_pid));

    let (done_tx, done_rx) = mpsc::channel();
    let stop_state = state.clone();
    let stop_thread = std::thread::spawn(move || {
        let result = stop_sidecar(stop_state, StopReason::Exit);
        let _ = done_tx.send(result);
    });
    let stop_result = match done_rx.recv_timeout(std::time::Duration::from_secs(4)) {
        Ok(result) => result,
        Err(error) => {
            kill_process_for_test_cleanup(descendant_pid);
            let _ = done_rx.recv_timeout(std::time::Duration::from_secs(3));
            let _ = stop_thread.join();
            panic!("stop blocked while descendant held output pipes: {error}");
        }
    };
    stop_thread.join().unwrap();
    let leader_gone = wait_until_process_is_gone(leader_pid);
    let descendant_gone = wait_until_process_is_gone(descendant_pid);
    if !descendant_gone {
        kill_process_for_test_cleanup(descendant_pid);
        let _ = wait_until_process_is_gone(descendant_pid);
    }

    assert_eq!(process_group_id, leader_pid);
    assert!(isolated_session);
    assert!(descendant_boundary);
    assert_ne!(process_group_id, unsafe { libc::getpgrp() });
    stop_result.expect("stop every process group in the sidecar session");
    assert!(leader_gone);
    assert!(descendant_gone);
}

#[cfg(target_os = "macos")]
#[tokio::test]
async fn exit_cleanup_uses_a_final_safe_session_kill_after_graceful_stop_fails() {
    let (_fixture, runtime) = write_lifecycle_fixture(None);
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    start_sidecar_core(state.clone(), runtime, true)
        .await
        .expect("start fixture");
    let leader_pid = state.lock().unwrap().sidecar_child.as_ref().unwrap().id() as i32;
    state.lock().unwrap().sidecar_process_group = Some(unsafe { libc::getpgrp() });

    let started = std::time::Instant::now();
    let result = super::shutdown_sidecar_for_exit(state.clone());
    let elapsed = started.elapsed();
    let snapshot = {
        let state = state.lock().unwrap();
        (
            state.sidecar_child.is_none(),
            state.sidecar_log_threads.is_empty(),
            state.session_token.is_empty(),
            state.desired_running,
            state.stopping,
        )
    };

    assert!(result.is_ok());
    assert!(elapsed < std::time::Duration::from_secs(8));
    assert!(wait_until_process_is_gone(leader_pid));
    assert_eq!(snapshot, (true, true, true, false, false));
}

#[cfg(target_os = "macos")]
#[tokio::test]
async fn exit_waits_for_concurrent_restart_stop_and_reaps_term_ignoring_descendant() {
    let (_fixture, mut runtime) = write_lifecycle_fixture(None);
    let descendant_pid_path = runtime.log_path.with_extension("exit-race-descendant.pid");
    runtime.extra_env.push((
        "PDF2MD_FIXTURE_DESCENDANT_PID".into(),
        descendant_pid_path.to_string_lossy().into_owned(),
    ));
    let restart_runtime = runtime.clone();
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    start_sidecar_core(state.clone(), runtime, true)
        .await
        .expect("start fixture with descendant");
    wait_for_nonempty_file(&descendant_pid_path).await;
    let descendant: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&descendant_pid_path).unwrap()).unwrap();
    let descendant_pid = descendant["pid"].as_i64().unwrap() as i32;
    let descendant_sid = descendant["sid"].as_i64().unwrap() as i32;
    let descendant_process_group = descendant["pgid"].as_i64().unwrap() as i32;
    let (leader_pid, sidecar_session_id, sidecar_process_group) = {
        let state = state.lock().unwrap();
        (
            state.sidecar_child.as_ref().unwrap().id() as i32,
            state.sidecar_session_id.unwrap(),
            state.sidecar_process_group.unwrap(),
        )
    };
    assert_eq!(sidecar_session_id, leader_pid);
    assert_eq!(descendant_sid, sidecar_session_id);
    assert_eq!(descendant_process_group, descendant_pid);
    assert_ne!(descendant_process_group, sidecar_process_group);

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    let mut restart = tokio::spawn(restart_sidecar_core(state.clone(), restart_runtime));
    if !wait_for_state_by(&state, |state| state.stopping, deadline).await {
        kill_process_for_test_cleanup(descendant_pid);
        kill_process_for_test_cleanup(leader_pid);
        panic!("concurrent restart stop did not acquire its owner lease");
    }
    assert!(process_exists(descendant_pid));

    let exit_state = state.clone();
    let mut exit =
        tokio::task::spawn_blocking(move || super::shutdown_sidecar_for_exit(exit_state));
    let exit_result = match tokio::time::timeout(
        deadline.saturating_duration_since(std::time::Instant::now()),
        &mut exit,
    )
    .await
    {
        Ok(result) => result.expect("exit cleanup task failed"),
        Err(error) => {
            kill_process_for_test_cleanup(descendant_pid);
            kill_process_for_test_cleanup(leader_pid);
            panic!("exit cleanup exceeded the five second deadline: {error}");
        }
    };
    let descendant_alive_when_exit_returned = process_exists(descendant_pid);
    let restart_result = tokio::time::timeout(
        deadline.saturating_duration_since(std::time::Instant::now()),
        &mut restart,
    )
    .await;
    let descendant_alive_after_results = process_exists(descendant_pid);
    let leader_alive_after_results = process_exists(leader_pid);
    if descendant_alive_after_results || leader_alive_after_results {
        let _ = super::force_terminate_stopping_identity(
            Some(sidecar_session_id),
            Some(sidecar_process_group),
        );
    }
    if process_exists(descendant_pid) {
        kill_process_for_test_cleanup(descendant_pid);
    }
    if process_exists(leader_pid) {
        kill_process_for_test_cleanup(leader_pid);
    }
    let descendant_gone = wait_until_process_is_gone_by(descendant_pid, deadline);
    let leader_gone = wait_until_process_is_gone_by(leader_pid, deadline);
    let restart_result = restart_result
        .expect("concurrent restart did not finish")
        .expect("concurrent restart task failed");
    let (session_is_empty, exit_intent_recorded) = {
        let state = state.lock().unwrap();
        (
            !state.stopping
                && state.sidecar_owner_generation.is_none()
                && state.sidecar_child.is_none()
                && state.sidecar_session_id.is_none()
                && state.sidecar_process_group.is_none()
                && state.sidecar_log_threads.is_empty()
                && state.session_token.is_empty(),
            !state.desired_running
                && state.service_state != "restarting"
                && state
                    .logs
                    .iter()
                    .any(|entry| entry == "[sidecar] application exit"),
        )
    };

    exit_result.expect("exit cleanup");
    assert!(matches!(restart_result, Err(SidecarStartError::Superseded)));
    assert!(leader_gone);
    assert!(descendant_gone);
    assert!(!leader_alive_after_results);
    assert!(!descendant_alive_after_results);
    assert!(session_is_empty);
    assert!(exit_intent_recorded);
    assert!(
        !descendant_alive_when_exit_returned,
        "exit cleanup returned while the sidecar descendant was still alive"
    );
}

#[cfg(target_os = "macos")]
#[tokio::test]
async fn application_exit_latch_blocks_pending_and_future_start_paths() {
    let control = std::env::temp_dir().join(format!(
        "pdf2md-exit-latch-{}-{}",
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

    let pending_start = tokio::spawn(start_sidecar_core(state.clone(), runtime.clone(), true));
    wait_for_nonempty_file(&control.join("started-1")).await;
    let first_pid: i32 = fs::read_to_string(control.join("started-1"))
        .unwrap()
        .parse()
        .unwrap();

    let exit_state = state.clone();
    let exit_result =
        tokio::task::spawn_blocking(move || super::shutdown_sidecar_for_exit(exit_state))
            .await
            .unwrap();
    let pending_result = tokio::time::timeout(std::time::Duration::from_secs(5), pending_start)
        .await
        .expect("pending startup did not stop after exit")
        .unwrap();
    let first_alive_when_exit_returned = process_exists(first_pid);
    let count_when_exit_returned: u64 = fs::read_to_string(control.join("counter"))
        .unwrap()
        .parse()
        .unwrap();

    for sequence in 2..=5 {
        fs::write(control.join(format!("gate-{sequence}")), b"continue").unwrap();
    }
    let direct_start = start_sidecar_core(state.clone(), runtime.clone(), true).await;
    if direct_start.is_ok() {
        let _ = stop_sidecar(state.clone(), StopReason::Exit);
    }
    {
        let mut state = state.lock().unwrap();
        prepare_retry(&mut state);
    }
    let retry_start = restart_sidecar_core(state.clone(), runtime.clone()).await;
    if retry_start.is_ok() {
        let _ = stop_sidecar(state.clone(), StopReason::Exit);
    }
    let _ = stop_sidecar(state.clone(), StopReason::Manual);
    {
        let mut state = state.lock().unwrap();
        prepare_retry(&mut state);
    }
    let manual_retry = start_sidecar_core(state.clone(), runtime, true).await;
    if manual_retry.is_ok() {
        let _ = stop_sidecar(state.clone(), StopReason::Exit);
    }
    let automatic_restart_requested = {
        let mut state = state.lock().unwrap();
        state.health_failures = super::MAX_HEALTH_FAILURES - 1;
        health_failure_requires_restart(&mut state)
    };
    let count_after_all_attempts: u64 = fs::read_to_string(control.join("counter"))
        .unwrap()
        .parse()
        .unwrap();

    if process_exists(first_pid) {
        let sid = unsafe { libc::getsid(first_pid) };
        let pgid = unsafe { libc::getpgid(first_pid) };
        if sid > 1 && pgid > 1 {
            let _ = super::force_terminate_stopping_identity(Some(sid), Some(pgid));
        }
    }

    exit_result.expect("exit cleanup");
    assert!(matches!(pending_result, Err(SidecarStartError::Superseded)));
    assert!(!first_alive_when_exit_returned);
    assert!(matches!(direct_start, Err(SidecarStartError::Superseded)));
    assert!(matches!(retry_start, Err(SidecarStartError::Superseded)));
    assert!(matches!(manual_retry, Err(SidecarStartError::Superseded)));
    assert!(!automatic_restart_requested);
    assert_eq!(count_when_exit_returned, 1);
    assert_eq!(count_after_all_attempts, count_when_exit_returned);
}

#[cfg(target_os = "macos")]
#[tokio::test]
async fn provisional_session_identity_reaps_descendant_after_leader_exits() {
    let root = std::env::temp_dir().join(format!(
        "pdf2md-provisional-identity-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir_all(&root).unwrap();
    let _cleanup = FixtureDirectory(root.clone());
    let descendant_path = root.join("descendant.json");
    let script = format!(
        "import json,os,signal,subprocess,sys; from pathlib import Path; \
         p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setpgrp); \
         Path({:?}).write_text(json.dumps({{'pid':p.pid,'sid':os.getsid(p.pid),'pgid':os.getpgid(p.pid)}}))",
        descendant_path.to_string_lossy().into_owned()
    );
    let mut command = std::process::Command::new("python3");
    command.args(["-c", &script]);
    command.stdin(std::process::Stdio::null());
    command.stdout(std::process::Stdio::null());
    command.stderr(std::process::Stdio::null());
    super::configure_sidecar_session(&mut command);
    let mut child = command.spawn().unwrap();
    let leader_pid = child.id() as i32;
    wait_for_nonempty_file(&descendant_path).await;
    let descendant: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&descendant_path).unwrap()).unwrap();
    let descendant_pid = descendant["pid"].as_i64().unwrap() as i32;
    let descendant_sid = descendant["sid"].as_i64().unwrap() as i32;
    let descendant_pgid = descendant["pgid"].as_i64().unwrap() as i32;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
    while child.try_wait().unwrap().is_none() && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    assert!(child.try_wait().unwrap().is_some());
    assert_eq!(descendant_sid, leader_pid);
    assert_eq!(descendant_pgid, descendant_pid);
    assert!(process_exists(descendant_pid));

    let state = Arc::new(Mutex::new(AppState::default()));
    let identity_result = {
        let mut current = state.lock().unwrap();
        let mut owner_slot = super::VacantOwnerSlot::new(&mut current).unwrap();
        super::ChildGuard::new(child, state.clone(), &mut owner_slot, 1)
    };
    let descendant_alive_when_registration_returned = process_exists(descendant_pid);

    if descendant_alive_when_registration_returned {
        let _ =
            super::force_terminate_stopping_identity(Some(descendant_sid), Some(descendant_pgid));
    }
    let descendant_gone = wait_until_process_is_gone(descendant_pid);

    assert!(identity_result.is_err());
    assert!(
        !descendant_alive_when_registration_returned,
        "identity registration failure leaked a provisional sidecar session descendant"
    );
    assert!(descendant_gone);
}

#[cfg(unix)]
#[test]
fn failed_provisional_termination_retains_child_for_later_exit_cleanup() {
    let mut command = std::process::Command::new("/bin/sleep");
    command.arg("30");
    super::configure_sidecar_session(&mut command);
    let child = command.spawn().expect("spawn isolated sidecar fixture");
    let child_pid = child.id() as i32;
    let state = Arc::new(Mutex::new(AppState {
        generation: 41,
        starting: true,
        desired_running: true,
        service_state: "starting".into(),
        session_token: "provisional-session-token-that-must-be-revoked".into(),
        ..Default::default()
    }));

    let first_cleanup = {
        let mut current = state.lock().unwrap();
        let mut owner_slot = super::VacantOwnerSlot::new(&mut current).unwrap();
        super::ChildGuard::new_with(
            child,
            state.clone(),
            &mut owner_slot,
            41,
            |_| {
                Err(std::io::Error::other(
                    "injected identity registration failure",
                ))
            },
            |_, _, _| {
                Err(std::io::Error::other(
                    "injected provisional session termination failure",
                ))
            },
        )
    };
    let retained = {
        let state = state.lock().unwrap();
        (
            state.sidecar_owner_generation,
            state.sidecar_child.as_ref().map(std::process::Child::id),
            state.sidecar_session_id,
            state.sidecar_process_group,
            state.session_token.clone(),
            state.service_state.clone(),
            state.error.as_ref().map(|error| error.category.clone()),
        )
    };
    let alive_after_first_failure = process_exists(child_pid);
    let second_cleanup = super::shutdown_sidecar_for_exit(state.clone());
    let gone_after_second_cleanup = wait_until_process_is_gone(child_pid);
    if !gone_after_second_cleanup {
        kill_process_for_test_cleanup(child_pid);
        let mut status = 0;
        let _ = unsafe { libc::waitpid(child_pid, &mut status, 0) };
    }

    assert!(matches!(
        first_cleanup,
        Err(ref error)
            if error.to_string().contains("injected identity registration failure")
                && error
                    .to_string()
                    .contains("injected provisional session termination failure")
    ));
    assert_eq!(retained.0, Some(41));
    assert_eq!(retained.1, Some(child_pid as u32));
    assert_eq!(retained.2, Some(child_pid));
    assert_eq!(retained.3, Some(child_pid));
    assert!(retained.4.is_empty());
    assert_eq!(retained.5, "failed");
    assert_eq!(retained.6.as_deref(), Some("shutdown"));
    assert!(alive_after_first_failure);
    assert!(second_cleanup.is_ok());
    assert!(gone_after_second_cleanup);
}

#[cfg(target_os = "macos")]
#[tokio::test]
async fn concurrent_manual_stop_remains_the_final_intent_during_exit_cleanup() {
    let (_fixture, mut runtime) = write_lifecycle_fixture(None);
    let descendant_pid_path = runtime
        .log_path
        .with_extension("manual-exit-race-descendant.pid");
    runtime
        .extra_env
        .push(("PDF2MD_FIXTURE_IGNORE_TERM".into(), "1".into()));
    runtime.extra_env.push((
        "PDF2MD_FIXTURE_DESCENDANT_PID".into(),
        descendant_pid_path.to_string_lossy().into_owned(),
    ));
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    start_sidecar_core(state.clone(), runtime, true)
        .await
        .expect("start fixture with descendant");
    wait_for_nonempty_file(&descendant_pid_path).await;
    let descendant: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&descendant_pid_path).unwrap()).unwrap();
    let descendant_pid = descendant["pid"].as_i64().unwrap() as i32;
    let leader_pid = state.lock().unwrap().sidecar_child.as_ref().unwrap().id() as i32;
    let initial_generation = state.lock().unwrap().generation;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);

    let manual_state = state.clone();
    let mut manual =
        tokio::task::spawn_blocking(move || stop_sidecar(manual_state, StopReason::Manual));
    if !wait_for_state_by(&state, |state| state.stopping, deadline).await {
        kill_process_for_test_cleanup(descendant_pid);
        kill_process_for_test_cleanup(leader_pid);
        panic!("manual stop did not acquire its owner lease");
    }

    let exit_state = state.clone();
    let mut exit =
        tokio::task::spawn_blocking(move || super::shutdown_sidecar_for_exit(exit_state));
    let exit_entered = wait_for_state_by(
        &state,
        |state| state.stopping && state.generation >= initial_generation + 2,
        deadline,
    )
    .await;
    let in_flight_intent = {
        let state = state.lock().unwrap();
        (
            state.manual_stopped,
            state.exit_stop_requested,
            state.desired_running,
        )
    };

    let exit_result = tokio::time::timeout(
        deadline.saturating_duration_since(std::time::Instant::now()),
        &mut exit,
    )
    .await;
    let manual_result = tokio::time::timeout(
        deadline.saturating_duration_since(std::time::Instant::now()),
        &mut manual,
    )
    .await;
    let descendant_alive_after_results = process_exists(descendant_pid);
    let leader_alive_after_results = process_exists(leader_pid);
    let sidecar_session_id = unsafe { libc::getsid(descendant_pid) };
    let descendant_process_group = unsafe { libc::getpgid(descendant_pid) };
    if descendant_alive_after_results || leader_alive_after_results {
        let _ = super::force_terminate_stopping_identity(
            Some(sidecar_session_id),
            Some(descendant_process_group),
        );
    }
    if process_exists(descendant_pid) {
        kill_process_for_test_cleanup(descendant_pid);
    }
    if process_exists(leader_pid) {
        kill_process_for_test_cleanup(leader_pid);
    }
    let descendant_gone = wait_until_process_is_gone_by(descendant_pid, deadline);
    let leader_gone = wait_until_process_is_gone_by(leader_pid, deadline);
    let final_state = {
        let state = state.lock().unwrap();
        (
            !state.stopping
                && state.sidecar_owner_generation.is_none()
                && state.sidecar_child.is_none()
                && state.sidecar_session_id.is_none()
                && state.sidecar_process_group.is_none()
                && state.sidecar_log_threads.is_empty()
                && state.session_token.is_empty(),
            state.manual_stopped,
            state.exit_stop_requested,
            state.desired_running,
            state.service_state.clone(),
            state.error.as_ref().map(|error| error.category.clone()),
            state
                .logs
                .iter()
                .any(|entry| entry == "[sidecar] application exit"),
        )
    };

    assert!(exit_entered, "exit did not overlap the manual stop");
    assert_eq!(
        in_flight_intent,
        (true, false, false),
        "exit must not record a lower-priority intent over manual stop"
    );
    exit_result
        .expect("exit cleanup exceeded the five second deadline")
        .expect("exit cleanup task failed")
        .expect("exit cleanup failed");
    manual_result
        .expect("manual stop exceeded the five second deadline")
        .expect("manual stop task failed")
        .expect("manual stop failed");
    assert!(leader_gone);
    assert!(descendant_gone);
    assert!(!leader_alive_after_results);
    assert!(!descendant_alive_after_results);
    assert_eq!(
        final_state,
        (
            true,
            true,
            false,
            false,
            "failed".into(),
            Some("manual_stop".into()),
            false,
        )
    );
}

#[test]
fn manual_stop_intent_clears_an_earlier_exit_intent() {
    let mut state = AppState::default();

    super::apply_stop_intent(&mut state, StopReason::Exit);
    assert!(state.exit_stop_requested);

    super::apply_stop_intent(&mut state, StopReason::Manual);

    assert!(state.manual_stopped);
    assert!(!state.desired_running);
    assert!(!state.exit_stop_requested);
    assert!(state.application_exiting);
}

#[cfg(unix)]
#[test]
fn logging_cleanup_spawn_failure_runs_inline_without_waiting_on_its_own_reader() {
    let child = std::process::Command::new("/bin/sleep")
        .arg("30")
        .spawn()
        .unwrap();
    let child_pid = child.id() as i32;
    let state = Arc::new(Mutex::new(AppState {
        generation: 71,
        sidecar_owner_generation: Some(71),
        sidecar_child: Some(child),
        running: false,
        service_state: "failed".into(),
        error: Some(crate::state::ServiceError {
            category: "logging".into(),
            message: "sidecar stderr logging failed".into(),
        }),
        logging_cleanup_scheduled: true,
        ..Default::default()
    }));
    let (start_tx, start_rx) = mpsc::channel();
    let (done_tx, done_rx) = mpsc::channel();
    let cleanup_state = state.clone();
    let reader = std::thread::spawn(move || {
        start_rx.recv().unwrap();
        super::schedule_logging_cleanup_with(cleanup_state, |_name, task| Err(task));
        done_tx.send(()).unwrap();
    });
    state.lock().unwrap().sidecar_log_threads.push(reader);
    start_tx.send(()).unwrap();

    let completed_without_deadlock = done_rx
        .recv_timeout(std::time::Duration::from_secs(3))
        .is_ok();
    let child_alive_when_cleanup_returned = process_exists(child_pid);
    let owner_cleared = {
        let state = state.lock().unwrap();
        state.sidecar_owner_generation.is_none()
            && state.sidecar_child.is_none()
            && state.sidecar_log_threads.is_empty()
            && !state.stopping
    };

    if child_alive_when_cleanup_returned {
        kill_process_for_test_cleanup(child_pid);
        let mut status = 0;
        let _ = unsafe { libc::waitpid(child_pid, &mut status, 0) };
    }

    assert!(completed_without_deadlock);
    assert!(!child_alive_when_cleanup_returned);
    assert!(owner_cleared);
}

#[cfg(unix)]
#[test]
fn startup_cleanup_spawn_failure_runs_inline_and_reaps_the_owner() {
    let child = std::process::Command::new("/bin/sleep")
        .arg("30")
        .spawn()
        .unwrap();
    let child_pid = child.id() as i32;
    let state = Arc::new(Mutex::new(AppState {
        generation: 72,
        sidecar_owner_generation: Some(72),
        sidecar_child: Some(child),
        starting: true,
        ..Default::default()
    }));

    super::schedule_startup_cleanup_with(state.clone(), 72, |_name, task| Err(task));

    let child_alive_when_cleanup_returned = process_exists(child_pid);
    let owner_cleared = {
        let state = state.lock().unwrap();
        state.sidecar_owner_generation.is_none()
            && state.sidecar_child.is_none()
            && state.sidecar_log_threads.is_empty()
            && !state.stopping
    };
    if child_alive_when_cleanup_returned {
        kill_process_for_test_cleanup(child_pid);
        let mut status = 0;
        let _ = unsafe { libc::waitpid(child_pid, &mut status, 0) };
    }

    assert!(!child_alive_when_cleanup_returned);
    assert!(owner_cleared);
}

#[cfg(target_os = "macos")]
#[tokio::test]
async fn exit_force_kills_a_stopping_session_before_a_delayed_owner_releases() {
    let (_fixture, mut runtime) = write_lifecycle_fixture(None);
    let descendant_pid_path = runtime
        .log_path
        .with_extension("delayed-owner-descendant.pid");
    runtime.extra_env.push((
        "PDF2MD_FIXTURE_DESCENDANT_PID".into(),
        descendant_pid_path.to_string_lossy().into_owned(),
    ));
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    start_sidecar_core(state.clone(), runtime, true)
        .await
        .expect("start fixture with descendant");
    wait_for_nonempty_file(&descendant_pid_path).await;
    let descendant: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&descendant_pid_path).unwrap()).unwrap();
    let descendant_pid = descendant["pid"].as_i64().unwrap() as i32;
    let descendant_sid = descendant["sid"].as_i64().unwrap() as i32;
    let descendant_process_group = descendant["pgid"].as_i64().unwrap() as i32;
    let (leader_pid, sidecar_session_id, sidecar_process_group, mut owner) = {
        let mut state = state.lock().unwrap();
        let leader_pid = state.sidecar_child.as_ref().unwrap().id() as i32;
        let sidecar_session_id = state.sidecar_session_id.unwrap();
        let sidecar_process_group = state.sidecar_process_group.unwrap();
        let owner = super::OwnedSidecar::take(&mut state);
        state.stopping = true;
        state.stopping_session_id = owner.session_id;
        state.stopping_process_group = owner.process_group_id;
        (leader_pid, sidecar_session_id, sidecar_process_group, owner)
    };
    assert_eq!(sidecar_session_id, leader_pid);
    assert_eq!(descendant_sid, sidecar_session_id);
    assert_eq!(descendant_process_group, descendant_pid);
    assert_ne!(descendant_process_group, sidecar_process_group);
    assert_ne!(sidecar_session_id, unsafe { libc::getsid(0) });
    assert_ne!(sidecar_process_group, unsafe { libc::getpgrp() });
    assert_ne!(descendant_process_group, unsafe { libc::getpgrp() });
    let mut unrelated = std::process::Command::new("/bin/sleep")
        .arg("30")
        .spawn()
        .unwrap();
    let unrelated_pid = unrelated.id() as i32;
    assert_ne!(unsafe { libc::getsid(unrelated_pid) }, sidecar_session_id);

    let started = std::time::Instant::now();
    let deadline = started + std::time::Duration::from_secs(5);
    let owner_release_at = started + std::time::Duration::from_millis(2_650);
    let owner_released = Arc::new(AtomicBool::new(false));
    let owner_released_for_task = owner_released.clone();
    let owner_state = state.clone();
    let mut delayed_owner = tokio::task::spawn_blocking(move || {
        std::thread::sleep(owner_release_at.saturating_duration_since(std::time::Instant::now()));
        if let Some(child) = owner.child.as_mut() {
            if child.try_wait().ok().flatten().is_none() {
                let _ = super::force_terminate_managed_child(
                    child,
                    owner.session_id,
                    owner.process_group_id,
                );
            }
            if child.try_wait().ok().flatten().is_none() {
                let _ = child.kill();
            }
            let _ = child.wait();
        }
        let readers = super::join_log_threads_bounded(std::mem::take(&mut owner.log_threads));
        let mut state = owner_state.lock().unwrap();
        state.sidecar_owner_generation = None;
        state.sidecar_child = None;
        state.sidecar_session_id = None;
        state.sidecar_process_group = None;
        state.sidecar_log_threads.clear();
        super::finish_empty_stop(&mut state, StopReason::Restart);
        if readers.detached > 0 {
            state.push_log(format!(
                "[sidecar] detached {} stale log reader(s) after process exit",
                readers.detached
            ));
        }
        drop(state);
        owner_released_for_task.store(true, Ordering::SeqCst);
        started.elapsed()
    });

    let exit_state = state.clone();
    let mut exit =
        tokio::task::spawn_blocking(move || super::shutdown_sidecar_for_exit(exit_state));
    let descendant_gone_before_release =
        wait_until_process_is_gone_by(descendant_pid, started + std::time::Duration::from_secs(2));
    let owner_was_held_when_force_completed = !owner_released.load(Ordering::SeqCst);
    let exit_result = tokio::time::timeout(
        deadline.saturating_duration_since(std::time::Instant::now()),
        &mut exit,
    )
    .await;
    let owner_release_elapsed = tokio::time::timeout(
        deadline.saturating_duration_since(std::time::Instant::now()),
        &mut delayed_owner,
    )
    .await;
    let descendant_alive_after_results = process_exists(descendant_pid);
    let leader_alive_after_results = process_exists(leader_pid);
    if descendant_alive_after_results || leader_alive_after_results {
        let _ = super::force_terminate_stopping_identity(
            Some(sidecar_session_id),
            Some(sidecar_process_group),
        );
    }
    if process_exists(descendant_pid) {
        kill_process_for_test_cleanup(descendant_pid);
    }
    if process_exists(leader_pid) {
        kill_process_for_test_cleanup(leader_pid);
    }
    let descendant_gone = wait_until_process_is_gone_by(descendant_pid, deadline);
    let leader_gone = wait_until_process_is_gone_by(leader_pid, deadline);
    let unrelated_survived = process_exists(unrelated_pid);
    let _ = unrelated.kill();
    let _ = unrelated.wait();
    let final_state_is_empty = {
        let state = state.lock().unwrap();
        !state.stopping
            && state.sidecar_owner_generation.is_none()
            && state.sidecar_child.is_none()
            && state.sidecar_session_id.is_none()
            && state.sidecar_process_group.is_none()
            && state.sidecar_log_threads.is_empty()
            && state.session_token.is_empty()
    };

    assert!(descendant_gone_before_release);
    assert!(owner_was_held_when_force_completed);
    exit_result
        .expect("exit cleanup exceeded the five second deadline")
        .expect("exit cleanup task failed")
        .expect("exit cleanup failed after forcing the stopping session");
    let owner_release_elapsed = owner_release_elapsed
        .expect("stop owner exceeded the five second deadline")
        .expect("stop owner task failed");
    assert!(owner_release_elapsed >= std::time::Duration::from_millis(2_500));
    assert!(owner_release_elapsed < std::time::Duration::from_secs(5));
    assert!(leader_gone);
    assert!(descendant_gone);
    assert!(!leader_alive_after_results);
    assert!(!descendant_alive_after_results);
    assert!(
        unrelated_survived,
        "exit cleanup killed an unrelated process"
    );
    assert!(final_state_is_empty);
}

#[cfg(unix)]
#[test]
fn process_group_safety_rejects_invalid_and_current_groups() {
    assert!(super::validate_process_group_id(0).is_err());
    assert!(super::validate_process_group_id(1).is_err());
    assert!(super::validate_process_group_id(unsafe { libc::getpgrp() }).is_err());
}

#[cfg(unix)]
#[tokio::test]
async fn failed_stop_retains_the_live_generation_and_blocks_replacement_start() {
    let child = std::process::Command::new("/bin/sleep")
        .arg("30")
        .spawn()
        .unwrap();
    let child_pid = child.id() as i32;
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let blocked_reader = std::thread::spawn(move || {
        let _ = release_rx.recv();
    });
    let (listener, port) = reserve_loopback_port().unwrap();
    let root = std::env::temp_dir().join(format!(
        "pdf2md-failed-stop-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let runtime = SidecarRuntime {
        script: root.join("missing-python3"),
        log_path: root.join("missing-parent").join("sidecar.log"),
        extra_env: Vec::new(),
        startup_timeout: std::time::Duration::from_secs(1),
    };
    let owner_generation = 21;
    let session = "live-session-0123456789abcdef0123456789abcdef";
    let state = Arc::new(Mutex::new(AppState {
        port,
        generation: owner_generation,
        session_token: session.into(),
        running: true,
        desired_running: true,
        service_state: "running".into(),
        sidecar_owner_generation: Some(owner_generation),
        sidecar_child: Some(child),
        sidecar_process_group: Some(unsafe { libc::getpgrp() }),
        sidecar_log_threads: vec![blocked_reader],
        reserved_listener: Some(listener),
        ..Default::default()
    }));

    let stop_result = stop_sidecar(state.clone(), StopReason::Restart);
    let generation_after_stop = state.lock().unwrap().generation;
    let replacement = start_sidecar_core(state.clone(), runtime, false).await;
    let snapshot = {
        let state = state.lock().unwrap();
        (
            state.sidecar_child.as_ref().map(std::process::Child::id),
            state.sidecar_process_group,
            state.sidecar_log_threads.len(),
            state.session_token.clone(),
            state.running,
            state.service_state.clone(),
            state.error.as_ref().map(|error| error.category.clone()),
            state.generation,
            ready_api_config(&state).is_ok(),
        )
    };
    let mut reader = std::io::Cursor::new(b"sidecar output after failed stop".to_vec());
    let mut writer = FailingLogWriter {
        message: "disk full after failed stop".into(),
    };
    super::copy_output_to_log(
        &mut reader,
        &mut writer,
        session.as_bytes(),
        &state,
        owner_generation,
        super::OutputStream::Stderr,
    )
    .unwrap_err();
    let logging_snapshot = {
        let state = state.lock().unwrap();
        (
            state.running,
            state.error.as_ref().map(|error| error.category.clone()),
        )
    };

    let _ = release_tx.send(());
    let retained_child = state.lock().unwrap().sidecar_child.is_some();
    if retained_child {
        state.lock().unwrap().sidecar_process_group = None;
        stop_sidecar(state.clone(), StopReason::Exit).unwrap();
    } else {
        kill_process_for_test_cleanup(child_pid);
        let mut status = 0;
        let _ = unsafe { libc::waitpid(child_pid, &mut status, 0) };
    }

    assert!(matches!(stop_result, Err(ref error) if error.contains("failed to stop")));
    assert!(matches!(replacement, Err(SidecarStartError::AlreadyActive)));
    assert_eq!(snapshot.0, Some(child_pid as u32));
    assert_eq!(snapshot.1, Some(unsafe { libc::getpgrp() }));
    assert_eq!(snapshot.2, 1);
    assert!(snapshot.3.is_empty());
    assert!(!snapshot.4);
    assert_eq!(snapshot.5, "failed");
    assert_eq!(snapshot.6.as_deref(), Some("shutdown"));
    assert_eq!(snapshot.7, generation_after_stop);
    assert!(!snapshot.8);
    assert!(!logging_snapshot.0);
    assert_eq!(logging_snapshot.1.as_deref(), Some("shutdown"));
}

#[tokio::test]
async fn dead_sidecar_detaches_a_stuck_reader_and_allows_a_new_generation() {
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let blocked_reader = std::thread::spawn(move || {
        let _ = release_rx.recv();
    });
    let (_fixture, runtime) = write_lifecycle_fixture(None);
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        generation: 31,
        session_token: "finished-process-session-0123456789abcdef".into(),
        running: true,
        desired_running: true,
        service_state: "running".into(),
        sidecar_log_threads: vec![blocked_reader],
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());

    let first_stop = stop_sidecar(state.clone(), StopReason::Restart);
    let retained_after_timeout = state.lock().unwrap().sidecar_log_threads.len();
    let replacement = start_sidecar_core(state.clone(), runtime, false).await;
    let replacement_running = state.lock().unwrap().running;
    let _ = release_tx.send(());
    if replacement.is_err() {
        let _ = stop_sidecar(state.clone(), StopReason::Exit);
    }

    assert!(first_stop.is_ok());
    assert_eq!(retained_after_timeout, 0);
    assert!(replacement.is_ok());
    assert!(replacement_running);
}

#[tokio::test(flavor = "current_thread")]
async fn async_stop_does_not_block_tokio_worker() {
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let blocked_reader = std::thread::spawn(move || {
        let _ = release_rx.recv();
    });
    let state = Arc::new(Mutex::new(AppState {
        generation: 41,
        desired_running: true,
        sidecar_log_threads: vec![blocked_reader],
        ..Default::default()
    }));
    let stop = tokio::spawn(stop_sidecar_async(state.clone(), StopReason::Restart));
    let worker_progress = tokio::spawn(async {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    });

    let progress =
        tokio::time::timeout(std::time::Duration::from_millis(250), worker_progress).await;
    let _ = release_tx.send(());
    let stop_result = stop.await.unwrap();
    if stop_result.is_err() {
        stop_sidecar(state, StopReason::Exit).unwrap();
    }

    assert!(
        progress.is_ok(),
        "synchronous stop blocked the Tokio worker"
    );
    assert!(stop_result.is_ok());
}

#[tokio::test(flavor = "current_thread")]
async fn async_restart_does_not_block_tokio_worker() {
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let blocked_reader = std::thread::spawn(move || {
        let _ = release_rx.recv();
    });
    let state = Arc::new(Mutex::new(AppState {
        generation: 42,
        desired_running: false,
        sidecar_log_threads: vec![blocked_reader],
        ..Default::default()
    }));
    let runtime = SidecarRuntime {
        script: PathBuf::from("unused-sidecar"),
        log_path: PathBuf::from("unused-sidecar.log"),
        extra_env: Vec::new(),
        startup_timeout: std::time::Duration::from_secs(1),
    };
    let restart = tokio::spawn(restart_sidecar_core(state.clone(), runtime));
    let worker_progress = tokio::spawn(async {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    });

    let progress =
        tokio::time::timeout(std::time::Duration::from_millis(250), worker_progress).await;
    let _ = release_tx.send(());
    let restart_result = restart.await.unwrap();
    if state.lock().unwrap().sidecar_log_threads.len() == 1 {
        stop_sidecar(state, StopReason::Exit).unwrap();
    }

    assert!(
        progress.is_ok(),
        "synchronous restart blocked the Tokio worker"
    );
    assert!(matches!(restart_result, Err(SidecarStartError::Superseded)));
}

#[tokio::test]
async fn manual_stop_intent_wins_while_automatic_restart_is_stopping() {
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let blocked_reader = std::thread::spawn(move || {
        let _ = release_rx.recv();
    });
    let root = std::env::temp_dir().join(format!(
        "pdf2md-manual-during-restart-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir_all(&root).unwrap();
    let _root_cleanup = FixtureDirectory(root.clone());
    let runtime = SidecarRuntime {
        script: root.join("missing-sidecar"),
        log_path: root.join("sidecar.log"),
        extra_env: Vec::new(),
        startup_timeout: std::time::Duration::from_secs(1),
    };
    let state = Arc::new(Mutex::new(AppState {
        generation: 42,
        running: true,
        desired_running: true,
        service_state: "running".into(),
        sidecar_log_threads: vec![blocked_reader],
        ..Default::default()
    }));
    let restart = tokio::spawn(restart_sidecar_core(state.clone(), runtime));
    assert!(wait_for_state(&state, |state| state.stopping).await);

    let manual = stop_sidecar(state.clone(), StopReason::Manual);
    let manual_returned_after_stop_completed = {
        let state = state.lock().unwrap();
        !state.stopping
            && state.sidecar_owner_generation.is_none()
            && state.sidecar_child.is_none()
            && state.sidecar_log_threads.is_empty()
    };
    let exit = super::shutdown_sidecar_for_exit(state.clone());
    let _ = release_tx.send(());
    let restart_result = restart.await.unwrap();
    let snapshot = {
        let state = state.lock().unwrap();
        (
            state.desired_running,
            state.manual_stopped,
            state.sidecar_child.is_none(),
            state.stopping,
        )
    };

    assert!(manual.is_ok());
    assert!(manual_returned_after_stop_completed);
    assert!(exit.is_ok());
    assert!(matches!(restart_result, Err(SidecarStartError::Superseded)));
    assert_eq!(snapshot, (false, true, true, false));
}

#[test]
fn manual_stop_reports_a_bounded_timeout_when_existing_stop_never_completes() {
    let state = Arc::new(Mutex::new(AppState {
        stopping: true,
        desired_running: true,
        running: true,
        ..Default::default()
    }));
    let started = std::time::Instant::now();

    let result = stop_sidecar(state, StopReason::Manual);

    assert!(matches!(
        result,
        Err(ref error) if error == "timed out waiting for concurrent sidecar shutdown after manual stop"
    ));
    assert!(started.elapsed() < std::time::Duration::from_secs(6));
}

#[cfg(unix)]
#[test]
fn owner_lease_recovers_poisoned_state_without_losing_child_ownership() {
    let state = Arc::new(Mutex::new(AppState {
        stopping: true,
        ..Default::default()
    }));
    let completion = state.lock().unwrap().stop_completed.clone();
    let poison_state = state.clone();
    let _ = std::thread::spawn(move || {
        let _guard = poison_state.lock().unwrap();
        panic!("poison state for owner lease regression");
    })
    .join();
    let child = std::process::Command::new("/bin/sleep")
        .arg("30")
        .spawn()
        .unwrap();
    let child_pid = child.id() as i32;

    drop(super::OwnerLease {
        state: state.clone(),
        owner: Some(super::OwnedSidecar {
            owner_generation: Some(91),
            child: Some(child),
            session_id: None,
            process_group_id: None,
            log_threads: Vec::new(),
            port: 0,
            session_token: String::new(),
        }),
        completion,
    });

    let mut recovered = match state.lock() {
        Ok(_) => panic!("state mutex was expected to remain poisoned"),
        Err(error) => error.into_inner(),
    };
    let ownership_restored = recovered.sidecar_owner_generation == Some(91)
        && recovered.sidecar_child.is_some()
        && !recovered.stopping;
    let restored_child = recovered.sidecar_child.take();
    drop(recovered);
    if let Some(mut child) = restored_child {
        let _ = child.kill();
        let _ = child.wait();
    } else {
        kill_process_for_test_cleanup(child_pid);
        let mut status = 0;
        let _ = unsafe { libc::waitpid(child_pid, &mut status, 0) };
    }

    assert!(ownership_restored);
}

#[cfg(unix)]
#[test]
fn poisoned_state_can_still_exit_and_reap_the_owned_child() {
    let child = std::process::Command::new("/bin/sleep")
        .arg("30")
        .spawn()
        .unwrap();
    let child_pid = child.id() as i32;
    let state = Arc::new(Mutex::new(AppState {
        generation: 92,
        sidecar_owner_generation: Some(92),
        sidecar_child: Some(child),
        running: true,
        desired_running: true,
        service_state: "running".into(),
        session_token: "poisoned-exit-token-that-must-be-revoked".into(),
        ..Default::default()
    }));
    poison_state(&state);

    let result = super::shutdown_sidecar_for_exit(state.clone());
    let gone_when_exit_returned = wait_until_process_is_gone(child_pid);
    let (owner_cleared, exiting, token_revoked, fallback_child) = {
        let mut state = match state.lock() {
            Ok(state) => state,
            Err(error) => error.into_inner(),
        };
        (
            state.sidecar_owner_generation.is_none()
                && state.sidecar_child.is_none()
                && state.sidecar_session_id.is_none()
                && state.sidecar_process_group.is_none(),
            state.application_exiting,
            state.session_token.is_empty(),
            state.sidecar_child.take(),
        )
    };
    if let Some(mut child) = fallback_child {
        let _ = child.kill();
        let _ = child.wait();
    } else if !gone_when_exit_returned {
        kill_process_for_test_cleanup(child_pid);
        let mut status = 0;
        let _ = unsafe { libc::waitpid(child_pid, &mut status, 0) };
    }

    assert!(result.is_ok());
    assert!(gone_when_exit_returned);
    assert!(owner_cleared);
    assert!(exiting);
    assert!(token_revoked);
}

#[cfg(unix)]
#[test]
fn startup_guard_recovers_poisoned_state_and_cleans_registered_child() {
    let child = std::process::Command::new("/bin/sleep")
        .arg("30")
        .spawn()
        .unwrap();
    let child_pid = child.id() as i32;
    let state = Arc::new(Mutex::new(AppState {
        generation: 93,
        sidecar_owner_generation: Some(93),
        sidecar_child: Some(child),
        starting: true,
        desired_running: true,
        service_state: "starting".into(),
        ..Default::default()
    }));
    poison_state(&state);

    drop(StartupGuard::new(state.clone(), 93));
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
    let cleaned = loop {
        let cleaned = {
            let state = match state.lock() {
                Ok(state) => state,
                Err(error) => error.into_inner(),
            };
            state.sidecar_owner_generation.is_none() && state.sidecar_child.is_none()
        };
        if cleaned || std::time::Instant::now() >= deadline {
            break cleaned;
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    };
    let gone_after_guard_cleanup = wait_until_process_is_gone(child_pid);
    let fallback_child = {
        let mut state = match state.lock() {
            Ok(state) => state,
            Err(error) => error.into_inner(),
        };
        state.sidecar_child.take()
    };
    if let Some(mut child) = fallback_child {
        let _ = child.kill();
        let _ = child.wait();
    } else if !gone_after_guard_cleanup {
        kill_process_for_test_cleanup(child_pid);
        let mut status = 0;
        let _ = unsafe { libc::waitpid(child_pid, &mut status, 0) };
    }

    assert!(cleaned);
    assert!(gone_after_guard_cleanup);
}

#[tokio::test]
async fn start_recovers_poisoned_state_and_commits_the_owner() {
    let (_fixture, runtime) = write_lifecycle_fixture(None);
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    poison_state(&state);

    let result = start_sidecar_core(state.clone(), runtime, true).await;
    let running_with_owner = {
        let state = match state.lock() {
            Ok(state) => state,
            Err(error) => error.into_inner(),
        };
        state.running
            && state.sidecar_owner_generation == Some(state.generation)
            && state.sidecar_child.is_some()
    };
    let cleanup = super::shutdown_sidecar_for_exit(state.clone());

    assert!(result.is_ok());
    assert!(running_with_owner);
    assert!(cleanup.is_ok());
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
    wait_for_nonempty_file(&control.join("started-1")).await;
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
    let (_fixture, mut runtime) = write_lifecycle_fixture(Some(&control));
    runtime
        .extra_env
        .push(("PDF2MD_FIXTURE_IGNORE_TERM".into(), "1".into()));
    let (listener, port) = reserve_loopback_port().unwrap();
    let state = Arc::new(Mutex::new(AppState {
        port,
        desired_running: true,
        reserved_listener: Some(listener),
        ..Default::default()
    }));
    let _cleanup = SidecarCleanup(state.clone());
    let startup = tokio::spawn(start_sidecar_core(state.clone(), runtime.clone(), true));
    wait_for_nonempty_file(&control.join("started-1")).await;
    let old_pid: i32 = fs::read_to_string(control.join("started-1"))
        .unwrap()
        .parse()
        .unwrap();

    let owner_registered_during_start = state.lock().unwrap().sidecar_child.is_some();
    let stop = tokio::spawn(stop_sidecar_async(state.clone(), StopReason::Manual));
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    let stop_waited_for_cleanup = !stop.is_finished();
    let replacement = start_sidecar_core(state.clone(), runtime, false).await;
    stop.await.unwrap().unwrap();
    let result = tokio::time::timeout(std::time::Duration::from_secs(5), startup)
        .await
        .expect("stale startup did not exit")
        .unwrap();

    assert!(matches!(result, Err(SidecarStartError::Superseded)));
    assert!(owner_registered_during_start);
    assert!(stop_waited_for_cleanup);
    assert!(matches!(replacement, Err(SidecarStartError::AlreadyActive)));
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
    wait_for_nonempty_file(&control.join("started-1")).await;
    let old_pid: i32 = fs::read_to_string(control.join("started-1"))
        .unwrap()
        .parse()
        .unwrap();
    let old_port = state.lock().unwrap().port;
    let retry = tokio::spawn(restart_sidecar_core(state.clone(), runtime));
    wait_for_nonempty_file(&control.join("started-2")).await;
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

#[test]
fn opening_preexisting_oversized_logs_enforces_both_hard_limits() {
    let root = std::env::temp_dir().join(format!(
        "pdf2md-preexisting-log-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir_all(&root).unwrap();
    let log_path = root.join("sidecar.log");
    let backup_path = root.join("sidecar.log.1");
    fs::write(&log_path, [b'a'; 150]).unwrap();
    fs::write(&backup_path, [b'b'; 150]).unwrap();

    drop(open_rotating_log(&log_path, 64).unwrap());

    assert!(fs::metadata(&log_path).unwrap().len() <= 64);
    assert!(fs::metadata(&backup_path).unwrap().len() <= 64);
    fs::remove_dir_all(root).unwrap();
}

#[cfg(unix)]
#[test]
fn symlink_log_target_is_rejected_without_touching_its_destination() {
    let root = std::env::temp_dir().join(format!(
        "pdf2md-symlink-log-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir_all(&root).unwrap();
    let destination = root.join("outside.log");
    let log_path = root.join("sidecar.log");
    fs::write(&destination, b"do-not-touch").unwrap();
    symlink(&destination, &log_path).unwrap();

    let error = match open_rotating_log(&log_path, 64) {
        Ok(_) => panic!("symlink log target must fail closed"),
        Err(error) => error,
    };

    assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
    assert_eq!(fs::read(&destination).unwrap(), b"do-not-touch");
    fs::remove_dir_all(root).unwrap();
}

#[cfg(unix)]
#[test]
fn log_directory_and_files_are_restricted_to_the_current_user() {
    let root = std::env::temp_dir().join(format!(
        "pdf2md-private-log-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let log_dir = root.join("logs");
    fs::create_dir_all(&log_dir).unwrap();
    fs::set_permissions(&log_dir, fs::Permissions::from_mode(0o755)).unwrap();
    let log_path = log_dir.join("sidecar.log");
    let backup_path = log_dir.join("sidecar.log.1");
    fs::write(&log_path, b"current").unwrap();
    fs::write(&backup_path, b"backup").unwrap();
    fs::set_permissions(&log_path, fs::Permissions::from_mode(0o644)).unwrap();
    fs::set_permissions(&backup_path, fs::Permissions::from_mode(0o644)).unwrap();

    prepare_private_log_directory(&log_dir).unwrap();
    drop(open_rotating_log(&log_path, 64).unwrap());

    assert_eq!(
        fs::metadata(&log_dir).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(&log_path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        fs::metadata(&backup_path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    fs::remove_dir_all(root).unwrap();
}

#[cfg(unix)]
#[test]
fn symlink_log_directory_is_rejected() {
    let root = std::env::temp_dir().join(format!(
        "pdf2md-symlink-log-dir-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    let destination = root.join("destination");
    let log_dir = root.join("logs");
    fs::create_dir_all(&destination).unwrap();
    symlink(&destination, &log_dir).unwrap();

    let error = prepare_private_log_directory(&log_dir).unwrap_err();

    assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
    fs::remove_dir_all(root).unwrap();
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
    assert!(
        wait_for_state(&state, |state| {
            state.sidecar_child.is_none() && state.sidecar_log_threads.is_empty()
        })
        .await
    );
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
fn reserved_socket_stays_close_on_exec_while_retaining_the_port() {
    let (listener, port) = reserve_loopback_port().expect("reserve port");
    make_socket_close_on_exec(&listener).expect("retain close-on-exec");

    let competitor = TcpListener::bind(("127.0.0.1", port));

    assert_eq!(
        competitor.unwrap_err().kind(),
        std::io::ErrorKind::AddrInUse
    );
    let flags = unsafe { libc::fcntl(listener.as_raw_fd(), libc::F_GETFD) };
    assert_ne!(flags & libc::FD_CLOEXEC, 0);
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
fn startup_failure_with_empty_session_preserves_the_actionable_message() {
    let state = Arc::new(Mutex::new(AppState {
        generation: 4,
        starting: true,
        service_state: "starting".into(),
        ..Default::default()
    }));
    let error = SidecarStartError::Failed {
        message: "failed to prepare private sidecar log directory".into(),
        generation: 4,
    };

    super::record_start_failure(&state, &error);

    let state = state.lock().unwrap();
    assert_eq!(state.service_state, "failed");
    assert_eq!(
        state.error.as_ref().map(|error| error.message.as_str()),
        Some("failed to prepare private sidecar log directory")
    );
    assert_eq!(
        state.logs.last().map(String::as_str),
        Some("[sidecar] configuration failure: failed to prepare private sidecar log directory")
    );
}

#[tokio::test]
async fn start_runtime_preparation_failure_sets_a_stable_failed_state() {
    let state = Arc::new(Mutex::new(AppState {
        generation: 5,
        starting: true,
        desired_running: true,
        service_state: "starting".into(),
        ..Default::default()
    }));

    let result = run_prepared_sidecar(
        state.clone(),
        Err("application support directory is unavailable".into()),
        SidecarAction::Start,
    )
    .await;

    assert_eq!(
        result.unwrap_err(),
        "application support directory is unavailable"
    );
    let state = state.lock().unwrap();
    assert!(!state.starting);
    assert!(!state.running);
    assert_eq!(state.service_state, "failed");
    let error = state.error.as_ref().expect("structured preparation error");
    assert_eq!(error.category, "configuration");
    assert_eq!(
        error.message,
        "application support directory is unavailable"
    );
}

#[tokio::test]
async fn restart_runtime_preparation_failure_stops_old_owner_and_redacts_its_session() {
    let secret = "restart-session-secret-0123456789abcdef0123456789abcdef";
    let state = Arc::new(Mutex::new(AppState {
        generation: 8,
        session_token: secret.into(),
        running: true,
        desired_running: true,
        service_state: "restarting".into(),
        ..Default::default()
    }));

    let result = run_prepared_sidecar(
        state.clone(),
        Err(format!("cannot prepare runtime for {secret}")),
        SidecarAction::Restart,
    )
    .await;

    let returned = result.unwrap_err();
    assert!(!returned.contains(secret));
    assert_eq!(returned, "cannot prepare runtime for [REDACTED]");
    let state = state.lock().unwrap();
    assert!(!state.starting);
    assert!(!state.running);
    assert!(state.session_token.is_empty());
    assert_eq!(state.service_state, "failed");
    let error = state.error.as_ref().expect("structured preparation error");
    assert_eq!(error.category, "configuration");
    assert_eq!(error.message, "cannot prepare runtime for [REDACTED]");
    assert!(!state.logs.join("\n").contains(secret));
}

#[tokio::test]
async fn restart_detaches_a_dead_reader_before_reporting_preparation_failure() {
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let blocked_reader = std::thread::spawn(move || {
        let _ = release_rx.recv();
    });
    let state = Arc::new(Mutex::new(AppState {
        generation: 15,
        session_token: "restart-secret-0123456789abcdef0123456789abcdef".into(),
        running: true,
        desired_running: true,
        service_state: "restarting".into(),
        sidecar_log_threads: vec![blocked_reader],
        ..Default::default()
    }));

    let result = run_prepared_sidecar(
        state.clone(),
        Err("replacement runtime is unavailable".into()),
        SidecarAction::Restart,
    )
    .await;
    let _ = release_tx.send(());

    let returned = result.unwrap_err();
    assert_eq!(returned, "replacement runtime is unavailable");
    let state = state.lock().unwrap();
    let error = state.error.as_ref().expect("structured preparation error");
    assert_eq!(error.category, "configuration");
    assert_eq!(error.message, "replacement runtime is unavailable");
    assert!(state.sidecar_log_threads.is_empty());
}

#[tokio::test]
async fn prepared_restart_detaches_a_dead_reader_and_starts_replacement() {
    let (_fixture, runtime) = write_lifecycle_fixture(None);
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let blocked_reader = std::thread::spawn(move || {
        let _ = release_rx.recv();
    });
    let state = Arc::new(Mutex::new(AppState {
        generation: 17,
        session_token: "prepared-restart-secret-0123456789abcdef".into(),
        running: true,
        desired_running: true,
        service_state: "running".into(),
        sidecar_log_threads: vec![blocked_reader],
        ..Default::default()
    }));

    let result = run_prepared_sidecar(state.clone(), Ok(runtime), SidecarAction::Restart).await;
    let snapshot = {
        let state = state.lock().unwrap();
        (
            state.service_state.clone(),
            state.error.as_ref().map(|error| error.category.clone()),
            state.error.as_ref().map(|error| error.message.clone()),
            state.sidecar_log_threads.len(),
        )
    };
    release_tx.send(()).unwrap();
    stop_sidecar(state, StopReason::Exit).unwrap();

    assert!(result.is_ok());
    assert_eq!(snapshot.0, "running");
    assert!(snapshot.1.is_none());
    assert!(snapshot.2.is_none());
    assert_eq!(snapshot.3, 2);
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
