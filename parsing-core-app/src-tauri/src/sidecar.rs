#[cfg(test)]
use crate::sidecar_log::MAX_READY_LINE_BYTES;
use crate::sidecar_log::{
    copy_redacted, open_rotating_log, prepare_private_log_directory, read_ready_line, SharedLog,
    MAX_LOG_BYTES,
};
use crate::state::{generate_session_token, AppState};
use serde::Deserialize;
use std::io::{BufReader, Read, Write};
use std::net::TcpListener;
use std::os::fd::{AsRawFd, RawFd};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

const HEALTH_INTERVAL_SECS: u64 = 3;
const HEALTH_STARTUP_GRACE_SECS: u64 = 60;
const MAX_HEALTH_FAILURES: u8 = 3;
const TERMINATION_TIMEOUT_MS: u64 = 2_000;
const LOG_THREAD_JOIN_TIMEOUT_MS: u64 = 1_000;
const SESSION_HEADER: &str = "X-PDF2MD-Session";
const SESSION_ENV: &str = "PDF2MD_SESSION_TOKEN";
const READY_SCHEMA: &str = "pdf2md.sidecar.ready.v1";
const MAX_HEALTH_RESPONSE_BYTES: usize = 4096;
const HEALTH_CONNECT_TIMEOUT_MS: u64 = 250;
const HEALTH_REQUEST_TIMEOUT_MS: u64 = 750;

#[derive(Clone)]
struct SidecarRuntime {
    script: PathBuf,
    log_path: PathBuf,
    extra_env: Vec<(String, String)>,
    startup_timeout: std::time::Duration,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReadyPayload {
    schema: String,
    host: String,
    pub port: u16,
}

pub fn parse_ready_line(line: &str, expected_port: u16) -> Result<ReadyPayload, String> {
    let ready: ReadyPayload =
        serde_json::from_str(line).map_err(|_| "invalid sidecar ready message".to_string())?;
    if ready.schema != READY_SCHEMA || ready.host != "127.0.0.1" || ready.port != expected_port {
        return Err("invalid sidecar ready message".into());
    }
    Ok(ready)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StopReason {
    Manual,
    Restart,
    Exit,
}

#[derive(Debug, Eq, PartialEq)]
enum SidecarStartError {
    AlreadyActive,
    Superseded,
    Failed { message: String, generation: u64 },
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum SidecarAction {
    Start,
    Restart,
}

#[derive(Clone, Copy)]
enum OutputStream {
    Stdout,
    Stderr,
}

impl OutputStream {
    fn label(self) -> &'static str {
        match self {
            Self::Stdout => "stdout",
            Self::Stderr => "stderr",
        }
    }
}

impl std::fmt::Display for SidecarStartError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::AlreadyActive => formatter.write_str("already running"),
            Self::Superseded => formatter.write_str("sidecar startup superseded"),
            Self::Failed { message, .. } => formatter.write_str(message),
        }
    }
}

fn record_start_failure(state: &Arc<Mutex<AppState>>, error: &SidecarStartError) -> String {
    let SidecarStartError::Failed {
        message,
        generation,
    } = error
    else {
        return error.to_string();
    };
    let Ok(mut state) = state.lock() else {
        return "sidecar startup failed".into();
    };
    let sanitized = sanitize_message(message, &state.session_token);
    if state.generation != *generation {
        return sanitized;
    }
    let category = classify_startup_error(&sanitized);
    state.starting = false;
    state.running = false;
    state.service_state = "failed".into();
    state.error = Some(crate::state::ServiceError {
        category: category.into(),
        message: sanitized.clone(),
    });
    state.push_log(format!("[sidecar] {category} failure: {sanitized}"));
    state.session_token.clear();
    sanitized
}

fn sanitize_message(message: &str, session_token: &str) -> String {
    if session_token.is_empty() {
        message.to_owned()
    } else {
        message.replace(session_token, "[REDACTED]")
    }
}

fn copy_output_to_log<R: Read, W: Write>(
    reader: &mut R,
    writer: &mut W,
    secret: &[u8],
    state: &Arc<Mutex<AppState>>,
    generation: u64,
    stream: OutputStream,
) -> std::io::Result<()> {
    let result = copy_redacted(reader, writer, secret);
    if let Err(error) = &result {
        if let Ok(mut state) = state.lock() {
            if state.generation == generation {
                let detail = sanitize_message(&error.to_string(), &state.session_token);
                let message = format!("sidecar {} logging failed", stream.label());
                state.service_state = "failed".into();
                state.error = Some(crate::state::ServiceError {
                    category: "logging".into(),
                    message,
                });
                state.push_log(format!(
                    "[sidecar] logging failure ({}): {detail}",
                    stream.label()
                ));
            }
        }
    }
    result
}

pub fn record_failure(
    state: &Arc<Mutex<AppState>>,
    generation: u64,
    category: &str,
    message: String,
) {
    if let Ok(mut s) = state.lock() {
        if s.generation != generation {
            return;
        }
        let message = sanitize_message(&message, &s.session_token);
        s.starting = false;
        s.running = false;
        s.service_state = "failed".into();
        s.error = Some(crate::state::ServiceError {
            category: category.into(),
            message: message.clone(),
        });
        s.push_log(format!("[sidecar] {category} failure: {message}"));
        s.session_token.clear();
    }
}

pub fn classify_startup_error(message: &str) -> &'static str {
    if message.contains("exited before becoming healthy") {
        "early_exit"
    } else if message.contains("healthy before timeout") {
        "health_timeout"
    } else if message.contains("failed to start") {
        "spawn"
    } else {
        "configuration"
    }
}

pub fn reserve_loopback_port() -> std::io::Result<(TcpListener, u16)> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    let port = listener.local_addr()?.port();
    Ok((listener, port))
}

fn reserve_loopback_port_excluding(excluded_port: u16) -> std::io::Result<(TcpListener, u16)> {
    let candidate = reserve_loopback_port()?;
    if excluded_port == 0 || candidate.1 != excluded_port {
        return Ok(candidate);
    }

    let replacement = reserve_loopback_port()?;
    drop(candidate);
    Ok(replacement)
}

pub fn make_socket_inheritable(listener: &TcpListener) -> std::io::Result<()> {
    let fd = listener.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags == -1 {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } == -1 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

fn sidecar_command(
    script: &Path,
    socket_fd: RawFd,
    parent_pid: u32,
    session_token: &str,
) -> Command {
    let mut command = Command::new("/bin/bash");
    command.arg(script).args([
        "--parent-pid",
        &parent_pid.to_string(),
        "--socket-fd",
        &socket_fd.to_string(),
    ]);
    command.env(SESSION_ENV, session_token);
    configure_sidecar_process_group(&mut command);
    command
}

fn configure_sidecar_process_group(command: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
}

pub struct StartupGuard {
    state: Arc<Mutex<AppState>>,
    generation: u64,
    committed: bool,
}

impl StartupGuard {
    pub fn new(state: Arc<Mutex<AppState>>, generation: u64) -> Self {
        Self {
            state,
            generation,
            committed: false,
        }
    }

    fn commit(mut self) {
        self.committed = true;
    }
}

impl Drop for StartupGuard {
    fn drop(&mut self) {
        if !self.committed {
            if let Ok(mut state) = self.state.lock() {
                if state.generation == self.generation {
                    state.starting = false;
                    state.running = false;
                }
            }
        }
    }
}

struct ChildGuard {
    child: Option<std::process::Child>,
    process_group_id: Option<i32>,
    log_threads: Vec<JoinHandle<()>>,
}

impl ChildGuard {
    fn new(mut child: std::process::Child) -> std::io::Result<Self> {
        let process_group_id = match sidecar_process_group_id(&child) {
            Ok(process_group_id) => process_group_id,
            Err(error) => {
                let _ = terminate_child(&mut child);
                return Err(error);
            }
        };
        Ok(Self {
            child: Some(child),
            process_group_id,
            log_threads: Vec::new(),
        })
    }

    fn child_mut(&mut self) -> &mut std::process::Child {
        self.child.as_mut().expect("child guard must contain child")
    }

    fn add_log_thread(&mut self, thread: JoinHandle<()>) {
        self.log_threads.push(thread);
    }

    fn take(mut self) -> (std::process::Child, Option<i32>, Vec<JoinHandle<()>>) {
        (
            self.child.take().expect("child guard must contain child"),
            self.process_group_id.take(),
            std::mem::take(&mut self.log_threads),
        )
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        if let Some(child) = self.child.as_mut() {
            let _ = terminate_managed_child(child, self.process_group_id);
        }
        let _ = join_log_threads_bounded(std::mem::take(&mut self.log_threads));
    }
}

#[cfg(unix)]
fn sidecar_process_group_id(child: &std::process::Child) -> std::io::Result<Option<i32>> {
    let pid = i32::try_from(child.id())
        .map_err(|_| std::io::Error::other("sidecar process id is out of range"))?;
    let process_group_id = unsafe { libc::getpgid(pid) };
    if process_group_id == -1 {
        return Err(std::io::Error::last_os_error());
    }
    let current_process_group = unsafe { libc::getpgrp() };
    if pid <= 1 || process_group_id != pid || process_group_id == current_process_group {
        return Err(std::io::Error::other(
            "sidecar did not start in an isolated process group",
        ));
    }
    Ok(Some(process_group_id))
}

#[cfg(not(unix))]
fn sidecar_process_group_id(_child: &std::process::Child) -> std::io::Result<Option<i32>> {
    Ok(None)
}

pub fn child_exited(child: &mut std::process::Child) -> std::io::Result<bool> {
    Ok(child.try_wait()?.is_some())
}

pub fn terminate_child(child: &mut std::process::Child) -> std::io::Result<()> {
    if !child_exited(child)? {
        if unsafe { libc::kill(child.id() as i32, libc::SIGTERM) } == -1 {
            return Err(std::io::Error::last_os_error());
        }
        let deadline =
            std::time::Instant::now() + std::time::Duration::from_millis(TERMINATION_TIMEOUT_MS);
        while std::time::Instant::now() < deadline {
            if child_exited(child)? {
                return Ok(());
            }
            std::thread::sleep(std::time::Duration::from_millis(25));
        }
        child.kill()?;
        child.wait()?;
    }
    Ok(())
}

fn terminate_managed_child(
    child: &mut std::process::Child,
    process_group_id: Option<i32>,
) -> std::io::Result<()> {
    #[cfg(unix)]
    if let Some(process_group_id) = process_group_id {
        return terminate_process_group(child, process_group_id);
    }
    terminate_child(child)
}

#[cfg(unix)]
fn terminate_process_group(
    child: &mut std::process::Child,
    process_group_id: i32,
) -> std::io::Result<()> {
    validate_process_group_id(process_group_id)?;
    signal_process_group(process_group_id, libc::SIGTERM)?;
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_millis(TERMINATION_TIMEOUT_MS);
    while std::time::Instant::now() < deadline {
        let leader_exited = child_exited(child)?;
        if leader_exited && !process_group_exists(process_group_id)? {
            return Ok(());
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }

    signal_process_group(process_group_id, libc::SIGKILL)?;
    let kill_deadline =
        std::time::Instant::now() + std::time::Duration::from_millis(TERMINATION_TIMEOUT_MS);
    while std::time::Instant::now() < kill_deadline {
        let leader_exited = child_exited(child)?;
        if leader_exited && !process_group_exists(process_group_id)? {
            return Ok(());
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }

    if !child_exited(child)? {
        child.kill()?;
        child.wait()?;
    }
    if process_group_exists(process_group_id)? {
        return Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "sidecar process group did not terminate",
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn validate_process_group_id(process_group_id: i32) -> std::io::Result<()> {
    if process_group_id <= 1 || process_group_id == unsafe { libc::getpgrp() } {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "refusing to signal an unsafe sidecar process group",
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn signal_process_group(process_group_id: i32, signal: i32) -> std::io::Result<()> {
    validate_process_group_id(process_group_id)?;
    if unsafe { libc::kill(-process_group_id, signal) } == 0 {
        return Ok(());
    }
    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(())
    } else {
        Err(error)
    }
}

#[cfg(unix)]
fn process_group_exists(process_group_id: i32) -> std::io::Result<bool> {
    validate_process_group_id(process_group_id)?;
    if unsafe { libc::kill(-process_group_id, 0) } == 0 {
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    match error.raw_os_error() {
        Some(libc::ESRCH) => Ok(false),
        Some(libc::EPERM) => Ok(true),
        _ => Err(error),
    }
}

fn join_log_threads_bounded(threads: Vec<JoinHandle<()>>) -> Result<(), String> {
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_millis(LOG_THREAD_JOIN_TIMEOUT_MS);
    while std::time::Instant::now() < deadline && threads.iter().any(|thread| !thread.is_finished())
    {
        std::thread::sleep(std::time::Duration::from_millis(10));
    }

    let mut unfinished = 0;
    let mut panicked = false;
    for thread in threads {
        if thread.is_finished() {
            panicked |= thread.join().is_err();
        } else {
            unfinished += 1;
        }
    }
    if unfinished > 0 {
        Err(format!(
            "{unfinished} sidecar log reader(s) did not stop before timeout"
        ))
    } else if panicked {
        Err("sidecar log reader stopped unexpectedly".into())
    } else {
        Ok(())
    }
}

fn build_health_client() -> Result<reqwest::Client, reqwest::Error> {
    reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(std::time::Duration::from_millis(HEALTH_CONNECT_TIMEOUT_MS))
        .timeout(std::time::Duration::from_millis(HEALTH_REQUEST_TIMEOUT_MS))
        .build()
}

fn health_client() -> Result<&'static reqwest::Client, String> {
    static CLIENT: std::sync::OnceLock<Result<reqwest::Client, String>> =
        std::sync::OnceLock::new();
    CLIENT
        .get_or_init(|| build_health_client().map_err(|_| "failed to build health client".into()))
        .as_ref()
        .map_err(Clone::clone)
}

async fn instance_is_healthy(client: &reqwest::Client, port: u16, token: &str) -> bool {
    let url = format!("http://127.0.0.1:{}/health", port);
    let Ok(mut response) = client.get(url).header(SESSION_HEADER, token).send().await else {
        return false;
    };
    if !response.status().is_success()
        || response
            .content_length()
            .is_some_and(|length| length > MAX_HEALTH_RESPONSE_BYTES as u64)
    {
        return false;
    }

    let mut body = Vec::with_capacity(64);
    loop {
        match response.chunk().await {
            Ok(Some(chunk)) => {
                if body.len() + chunk.len() > MAX_HEALTH_RESPONSE_BYTES {
                    return false;
                }
                body.extend_from_slice(&chunk);
            }
            Ok(None) => break,
            Err(_) => return false,
        }
    }
    serde_json::from_slice::<serde_json::Value>(&body)
        .ok()
        .and_then(|value| {
            value
                .get("status")
                .and_then(|status| status.as_str())
                .map(str::to_owned)
        })
        .as_deref()
        == Some("ok")
}

fn bundled_sidecar_runtime() -> Result<SidecarRuntime, String> {
    let exe = std::env::current_exe().map_err(|error| error.to_string())?;
    let script = exe
        .parent()
        .ok_or_else(|| "missing app executable directory".to_string())?
        .join("python3");
    let log_dir = dirs::data_dir()
        .ok_or_else(|| "missing application support directory".to_string())?
        .join("PDF2MD")
        .join("logs");
    prepare_private_log_directory(&log_dir)
        .map_err(|error| format!("failed to create log dir {}: {error}", log_dir.display()))?;
    Ok(SidecarRuntime {
        script,
        log_path: log_dir.join("sidecar.log"),
        extra_env: Vec::new(),
        startup_timeout: std::time::Duration::from_secs(HEALTH_STARTUP_GRACE_SECS),
    })
}

fn generation_is_current(state: &Arc<Mutex<AppState>>, generation: u64) -> bool {
    state
        .lock()
        .map(|state| state.generation == generation)
        .unwrap_or(false)
}

fn failed_or_superseded(
    state: &Arc<Mutex<AppState>>,
    generation: u64,
    message: impl Into<String>,
) -> SidecarStartError {
    if generation_is_current(state, generation) {
        SidecarStartError::Failed {
            message: message.into(),
            generation,
        }
    } else {
        SidecarStartError::Superseded
    }
}

async fn start_sidecar_core(
    state: Arc<Mutex<AppState>>,
    runtime: SidecarRuntime,
    restore_desired_running: bool,
) -> Result<(), SidecarStartError> {
    let (listener, port, session_token, generation) = {
        let mut s = state.lock().map_err(|error| SidecarStartError::Failed {
            message: error.to_string(),
            generation: 0,
        })?;
        if s.running || s.starting {
            return Err(SidecarStartError::AlreadyActive);
        }
        if restore_desired_running {
            s.desired_running = true;
            s.manual_stopped = false;
        } else if !s.desired_running || s.manual_stopped {
            return Err(SidecarStartError::Superseded);
        }
        let (listener, port) = match s.reserved_listener.take() {
            Some(listener) => {
                let port = listener
                    .local_addr()
                    .map_err(|error| SidecarStartError::Failed {
                        message: error.to_string(),
                        generation: s.generation,
                    })?
                    .port();
                (listener, port)
            }
            None => reserve_loopback_port_excluding(s.port).map_err(|error| {
                SidecarStartError::Failed {
                    message: error.to_string(),
                    generation: s.generation,
                }
            })?,
        };
        s.generation = s.generation.wrapping_add(1).max(1);
        let generation = s.generation;
        s.starting = true;
        s.service_state = "starting".into();
        s.error = None;
        s.port = port;
        s.session_token = generate_session_token();
        (listener, port, s.session_token.clone(), generation)
    };
    let startup_guard = StartupGuard::new(state.clone(), generation);
    make_socket_inheritable(&listener).map_err(|error| {
        failed_or_superseded(
            &state,
            generation,
            format!("failed to inherit sidecar socket: {error}"),
        )
    })?;

    let parent_pid = std::process::id();
    if let Ok(mut s) = state.lock() {
        if s.generation == generation {
            s.log_path = Some(runtime.log_path.to_string_lossy().into_owned());
        }
    }
    let log = open_rotating_log(&runtime.log_path, MAX_LOG_BYTES).map_err(|error| {
        failed_or_superseded(
            &state,
            generation,
            format!("failed to open {}: {error}", runtime.log_path.display()),
        )
    })?;
    let shared_log = SharedLog::new(log);
    let mut command = sidecar_command(
        &runtime.script,
        listener.as_raw_fd(),
        parent_pid,
        &session_token,
    );
    command.envs(runtime.extra_env.iter().cloned());
    let spawn_result = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();
    let child = match spawn_result {
        Ok(child) => child,
        Err(error) => {
            return Err(failed_or_superseded(
                &state,
                generation,
                format!("failed to start {}: {error}", runtime.script.display()),
            ));
        }
    };
    let mut child_guard = ChildGuard::new(child).map_err(|error| {
        failed_or_superseded(
            &state,
            generation,
            format!("failed to isolate sidecar process: {error}"),
        )
    })?;

    let child_stdout =
        child_guard.child_mut().stdout.take().ok_or_else(|| {
            failed_or_superseded(&state, generation, "sidecar stdout unavailable")
        })?;
    let child_stderr =
        child_guard.child_mut().stderr.take().ok_or_else(|| {
            failed_or_superseded(&state, generation, "sidecar stderr unavailable")
        })?;
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    let mut stdout_log = shared_log.clone();
    let stdout_secret = session_token.clone();
    let stdout_state = state.clone();
    let stdout_thread = std::thread::Builder::new()
        .name("sidecar-stdout".into())
        .spawn(move || {
            let mut reader = BufReader::new(child_stdout);
            let ready = read_ready_line(&mut reader);
            let _ = ready_tx.send(ready);
            let _ = copy_output_to_log(
                &mut reader,
                &mut stdout_log,
                stdout_secret.as_bytes(),
                &stdout_state,
                generation,
                OutputStream::Stdout,
            );
        })
        .map_err(|_| {
            failed_or_superseded(&state, generation, "failed to start sidecar output reader")
        })?;
    child_guard.add_log_thread(stdout_thread);
    let mut stderr_log = shared_log;
    let stderr_secret = session_token.clone();
    let stderr_state = state.clone();
    let stderr_thread = std::thread::Builder::new()
        .name("sidecar-stderr".into())
        .spawn(move || {
            let mut reader = BufReader::new(child_stderr);
            let _ = copy_output_to_log(
                &mut reader,
                &mut stderr_log,
                stderr_secret.as_bytes(),
                &stderr_state,
                generation,
                OutputStream::Stderr,
            );
        })
        .map_err(|_| {
            failed_or_superseded(&state, generation, "failed to start sidecar error reader")
        })?;
    child_guard.add_log_thread(stderr_thread);

    let ready_deadline = tokio::time::Instant::now() + runtime.startup_timeout;
    let mut ready_rx = std::pin::pin!(ready_rx);
    let ready_line = loop {
        if !generation_is_current(&state, generation) {
            return Err(SidecarStartError::Superseded);
        }
        if child_exited(child_guard.child_mut())
            .map_err(|error| failed_or_superseded(&state, generation, error.to_string()))?
        {
            return Err(failed_or_superseded(
                &state,
                generation,
                "sidecar exited before becoming healthy",
            ));
        }
        if tokio::time::Instant::now() >= ready_deadline {
            return Err(failed_or_superseded(
                &state,
                generation,
                "sidecar did not emit ready message before timeout",
            ));
        }
        tokio::select! {
            result = &mut ready_rx => {
                break result
                    .map_err(|_| failed_or_superseded(
                        &state,
                        generation,
                        "sidecar ready channel closed",
                    ))?
                    .map_err(|message| failed_or_superseded(&state, generation, message))?;
            }
            _ = tokio::time::sleep(std::time::Duration::from_millis(25)) => {}
        }
    };
    if !generation_is_current(&state, generation) {
        return Err(SidecarStartError::Superseded);
    }
    let ready = parse_ready_line(&ready_line, port)
        .map_err(|message| failed_or_superseded(&state, generation, message))?;

    let client =
        health_client().map_err(|message| failed_or_superseded(&state, generation, message))?;
    let deadline = tokio::time::Instant::now() + runtime.startup_timeout;
    loop {
        if !generation_is_current(&state, generation) {
            return Err(SidecarStartError::Superseded);
        }
        if child_exited(child_guard.child_mut())
            .map_err(|error| failed_or_superseded(&state, generation, error.to_string()))?
        {
            return Err(failed_or_superseded(
                &state,
                generation,
                "sidecar exited before becoming healthy",
            ));
        }
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(failed_or_superseded(
                &state,
                generation,
                "sidecar did not become healthy before timeout",
            ));
        }
        let healthy = tokio::time::timeout(
            deadline - now,
            instance_is_healthy(client, ready.port, &session_token),
        )
        .await
        .unwrap_or(false);
        if !generation_is_current(&state, generation) {
            return Err(SidecarStartError::Superseded);
        }
        if healthy {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }

    drop(listener);
    {
        let mut s = state.lock().map_err(|error| SidecarStartError::Failed {
            message: error.to_string(),
            generation,
        })?;
        if s.generation != generation || !s.desired_running || s.manual_stopped {
            return Err(SidecarStartError::Superseded);
        }
        let (child, process_group_id, log_threads) = child_guard.take();
        s.sidecar_child = Some(child);
        s.sidecar_process_group = process_group_id;
        s.sidecar_log_threads = log_threads;
        s.port = ready.port;
        s.starting = false;
        s.running = true;
        s.service_state = "running".into();
        s.push_log(format!(
            "[sidecar] started on port {}, log {}",
            ready.port,
            runtime.log_path.display()
        ));
    }
    startup_guard.commit();

    Ok(())
}

async fn run_prepared_sidecar(
    state: Arc<Mutex<AppState>>,
    runtime: Result<SidecarRuntime, String>,
    action: SidecarAction,
) -> Result<(), String> {
    let runtime = match runtime {
        Ok(runtime) => runtime,
        Err(message) => {
            let sanitized = state
                .lock()
                .map(|state| sanitize_message(&message, &state.session_token))
                .unwrap_or_else(|_| "failed to prepare sidecar runtime".into());
            if action == SidecarAction::Restart {
                stop_sidecar(state.clone(), StopReason::Restart)?;
            }
            let generation = state.lock().map(|state| state.generation).unwrap_or(0);
            let error = SidecarStartError::Failed {
                message: sanitized,
                generation,
            };
            return Err(record_start_failure(&state, &error));
        }
    };

    let result = match action {
        SidecarAction::Start => start_sidecar_core(state.clone(), runtime, true).await,
        SidecarAction::Restart => restart_sidecar_core(state.clone(), runtime).await,
    };
    match result {
        Ok(()) => Ok(()),
        Err(error) => Err(record_start_failure(&state, &error)),
    }
}

pub async fn start_sidecar(
    _app: &tauri::AppHandle,
    state: Arc<Mutex<AppState>>,
) -> Result<(), String> {
    run_prepared_sidecar(state, bundled_sidecar_runtime(), SidecarAction::Start).await
}

pub fn prepare_retry(state: &mut AppState) {
    state.desired_running = true;
    state.manual_stopped = false;
    state.service_state = "restarting".into();
    state.error = None;
    state.health_failures = 0;
}

pub fn health_failure_requires_restart(state: &mut AppState) -> bool {
    if !state.desired_running || state.manual_stopped {
        state.health_failures = 0;
        return false;
    }
    state.health_failures = state.health_failures.saturating_add(1);
    if state.health_failures < MAX_HEALTH_FAILURES {
        return false;
    }
    state.service_state = "restarting".into();
    state.push_log("[health] 3 failures, restarting sidecar...");
    true
}

pub fn stop_sidecar(state: Arc<Mutex<AppState>>, reason: StopReason) -> Result<(), String> {
    let (mut child, process_group_id, log_threads) = {
        let mut s = state.lock().map_err(|error| error.to_string())?;
        s.generation = s.generation.wrapping_add(1).max(1);
        let child = s.sidecar_child.take();
        let process_group_id = s.sidecar_process_group.take();
        let log_threads = std::mem::take(&mut s.sidecar_log_threads);
        if child.is_some() {
            s.push_log("[sidecar] stopped");
        }
        s.running = false;
        s.starting = false;
        s.health_failures = 0;
        s.session_token.clear();
        match reason {
            StopReason::Manual => {
                s.desired_running = false;
                s.manual_stopped = true;
                s.service_state = "failed".into();
                s.error = Some(crate::state::ServiceError {
                    category: "manual_stop".into(),
                    message: "service stopped by user".into(),
                });
            }
            StopReason::Exit => {
                s.desired_running = false;
                s.manual_stopped = false;
                s.push_log("[sidecar] application exit");
            }
            StopReason::Restart => {
                s.service_state = "restarting".into();
            }
        }
        (child, process_group_id, log_threads)
    };
    let termination = match child.as_mut() {
        Some(child) => terminate_managed_child(child, process_group_id)
            .map_err(|error| format!("failed to stop sidecar process group: {error}")),
        None => Ok(()),
    };
    let readers = join_log_threads_bounded(log_threads);
    let cleanup = termination.and(readers);
    if let Err(message) = cleanup {
        if let Ok(mut state) = state.lock() {
            state.service_state = "failed".into();
            state.error = Some(crate::state::ServiceError {
                category: "shutdown".into(),
                message: message.clone(),
            });
            state.push_log(format!("[sidecar] shutdown failure: {message}"));
        }
        return Err(message);
    }
    Ok(())
}

async fn restart_sidecar_core(
    state: Arc<Mutex<AppState>>,
    runtime: SidecarRuntime,
) -> Result<(), SidecarStartError> {
    stop_sidecar(state.clone(), StopReason::Restart).map_err(|message| {
        let generation = state.lock().map(|state| state.generation).unwrap_or(0);
        SidecarStartError::Failed {
            message,
            generation,
        }
    })?;
    start_sidecar_core(state, runtime, false).await
}

pub async fn restart_sidecar(
    _app: &tauri::AppHandle,
    state: Arc<Mutex<AppState>>,
) -> Result<(), String> {
    run_prepared_sidecar(state, bundled_sidecar_runtime(), SidecarAction::Restart).await
}

pub async fn retry_sidecar(
    app: &tauri::AppHandle,
    state: Arc<Mutex<AppState>>,
) -> Result<(), String> {
    {
        let mut s = state.lock().map_err(|e| e.to_string())?;
        prepare_retry(&mut s);
    }
    restart_sidecar(app, state).await
}

pub async fn health_loop(app: tauri::AppHandle, state: Arc<Mutex<AppState>>) {
    let client = match health_client() {
        Ok(client) => client,
        Err(error) => {
            let generation = state.lock().map(|state| state.generation).unwrap_or(0);
            record_failure(&state, generation, "configuration", error);
            return;
        }
    };
    tokio::time::sleep(std::time::Duration::from_secs(HEALTH_STARTUP_GRACE_SECS)).await;
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(HEALTH_INTERVAL_SECS)).await;
        let (port, token, generation) = {
            let s = state.lock().unwrap();
            if !s.desired_running || s.manual_stopped || !s.running {
                continue;
            }
            (s.port, s.session_token.clone(), s.generation)
        };
        match instance_is_healthy(client, port, &token).await {
            true => {
                let mut state = state.lock().unwrap();
                if state.generation == generation {
                    state.health_failures = 0;
                }
            }
            false => {
                let should_restart = {
                    let mut s = state.lock().unwrap();
                    s.generation == generation && health_failure_requires_restart(&mut s)
                };
                if should_restart {
                    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                    if state.lock().unwrap().generation != generation {
                        continue;
                    }
                    let _ = restart_sidecar(&app, state.clone()).await;
                    tokio::time::sleep(std::time::Duration::from_secs(HEALTH_STARTUP_GRACE_SECS))
                        .await;
                }
            }
        }
    }
}

#[cfg(test)]
#[path = "sidecar_tests.rs"]
mod tests;
