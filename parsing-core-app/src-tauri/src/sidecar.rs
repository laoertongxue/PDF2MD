#[cfg(test)]
use crate::sidecar_log::MAX_READY_LINE_BYTES;
use crate::sidecar_log::{
    copy_redacted, open_rotating_log, prepare_private_log_directory, read_ready_line, SharedLog,
    MAX_LOG_BYTES,
};
use crate::state::{generate_session_token, lock_state, AppState};
use serde::Deserialize;
use std::fs::File;
use std::io::{BufReader, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

const HEALTH_INTERVAL_SECS: u64 = 3;
const HEALTH_STARTUP_GRACE_SECS: u64 = 60;
const MAX_HEALTH_FAILURES: u8 = 3;
const TERMINATION_TIMEOUT_MS: u64 = 2_000;
const LOG_THREAD_JOIN_TIMEOUT_MS: u64 = 1_000;
const EXIT_STOP_WAIT_TIMEOUT_MS: u64 = 500;
const EXIT_STOP_FORCE_TIMEOUT_MS: u64 = 1_000;
const EXIT_STOP_RELEASE_TIMEOUT_MS: u64 = 3_000;
const MANUAL_STOP_WAIT_TIMEOUT_MS: u64 = 5_000;
const SESSION_HEADER: &str = "X-PDF2MD-Session";
const SESSION_ENV: &str = "PDF2MD_SESSION_TOKEN";
const READY_SCHEMA: &str = "pdf2md.sidecar.ready.v1";
const MAX_HEALTH_RESPONSE_BYTES: usize = 4096;
const HEALTH_CONNECT_TIMEOUT_MS: u64 = 250;
const HEALTH_REQUEST_TIMEOUT_MS: u64 = 750;
const SHUTDOWN_REQUEST_TIMEOUT_MS: u64 = 750;
const SHUTDOWN_EXIT_TIMEOUT_MS: u64 = 750;
const MAX_SHUTDOWN_RESPONSE_BYTES: usize = 4096;
const CHILD_SOCKET_FD: RawFd = 3;
const CHILD_SESSION_TOKEN_FD: RawFd = 4;
const CHILD_DUPLICATE_FD_MIN: RawFd = 64;

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
    LoggingFailure,
    StartupFailure,
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
    let mut state = lock_state(state);
    let sanitized = sanitize_message(message, &state.session_token);
    if state.application_exiting {
        return sanitized;
    }
    if state.generation != *generation {
        return sanitized;
    }
    if state
        .error
        .as_ref()
        .is_some_and(|error| matches!(error.category.as_str(), "logging" | "shutdown"))
    {
        state.starting = false;
        state.running = false;
        state.session_token.clear();
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
    let mut schedule_cleanup = false;
    if let Err(error) = &result {
        let mut state = lock_state(state);
        if state.generation == generation || state.sidecar_owner_generation == Some(generation) {
            let detail = sanitize_message(&error.to_string(), &state.session_token);
            let message = format!("sidecar {} logging failed", stream.label());
            state.running = false;
            let preserve_shutdown = state
                .error
                .as_ref()
                .is_some_and(|error| error.category == "shutdown");
            if !preserve_shutdown {
                state.service_state = "failed".into();
                state.error = Some(crate::state::ServiceError {
                    category: "logging".into(),
                    message,
                });
            }
            state.push_log(format!(
                "[sidecar] logging failure ({}): {detail}",
                stream.label()
            ));
            state.session_token.clear();
            if state.sidecar_owner_generation == Some(generation)
                && !state.logging_cleanup_scheduled
            {
                state.logging_cleanup_scheduled = true;
                schedule_cleanup = true;
            }
        }
    }
    if schedule_cleanup {
        schedule_logging_cleanup(state.clone());
    }
    result
}

pub fn record_failure(
    state: &Arc<Mutex<AppState>>,
    generation: u64,
    category: &str,
    message: String,
) {
    let mut s = lock_state(state);
    if s.generation != generation || s.application_exiting {
        return;
    }
    if s.error
        .as_ref()
        .is_some_and(|error| error.category == "shutdown")
    {
        s.starting = false;
        s.running = false;
        s.session_token.clear();
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

fn record_shutdown_failure_locked(state: &mut AppState, message: &str) -> String {
    let message = sanitize_message(message, &state.session_token);
    state.starting = false;
    state.running = false;
    state.service_state = "failed".into();
    state.error = Some(crate::state::ServiceError {
        category: "shutdown".into(),
        message: message.clone(),
    });
    state.session_token.clear();
    state.push_log(format!("[sidecar] shutdown failure: {message}"));
    message
}

fn record_shutdown_failure(state: &Arc<Mutex<AppState>>, message: &str) -> String {
    record_shutdown_failure_locked(&mut lock_state(state), message)
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

pub fn make_socket_close_on_exec(listener: &TcpListener) -> std::io::Result<()> {
    make_fd_close_on_exec(listener.as_raw_fd())
}

fn make_fd_close_on_exec(fd: RawFd) -> std::io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags == -1 {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } == -1 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

fn session_token_channel(session_token: &str) -> std::io::Result<File> {
    let mut descriptors = [-1; 2];
    if unsafe { libc::pipe(descriptors.as_mut_ptr()) } == -1 {
        return Err(std::io::Error::last_os_error());
    }
    let reader = unsafe { File::from_raw_fd(descriptors[0]) };
    let mut writer = unsafe { File::from_raw_fd(descriptors[1]) };
    let result = (|| {
        let reader_flags = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_GETFD) };
        if reader_flags == -1 {
            return Err(std::io::Error::last_os_error());
        }
        if unsafe {
            libc::fcntl(
                reader.as_raw_fd(),
                libc::F_SETFD,
                reader_flags | libc::FD_CLOEXEC,
            )
        } == -1
        {
            return Err(std::io::Error::last_os_error());
        }
        let writer_flags = unsafe { libc::fcntl(writer.as_raw_fd(), libc::F_GETFD) };
        if writer_flags == -1 {
            return Err(std::io::Error::last_os_error());
        }
        if unsafe {
            libc::fcntl(
                writer.as_raw_fd(),
                libc::F_SETFD,
                writer_flags | libc::FD_CLOEXEC,
            )
        } == -1
        {
            return Err(std::io::Error::last_os_error());
        }
        writer.write_all(session_token.as_bytes())?;
        writer.flush()
    })();
    drop(writer);
    result.map(|()| reader)
}

fn sidecar_command(
    script: &Path,
    socket_fd: RawFd,
    session_token_fd: RawFd,
    parent_pid: u32,
) -> Command {
    let mut command = Command::new("/bin/bash");
    command.arg(script).args([
        "--parent-pid",
        &parent_pid.to_string(),
        "--socket-fd",
        &CHILD_SOCKET_FD.to_string(),
        "--session-token-fd",
        &CHILD_SESSION_TOKEN_FD.to_string(),
    ]);
    command.env_remove(SESSION_ENV);
    configure_inherited_sidecar_fds(&mut command, socket_fd, session_token_fd);
    configure_sidecar_session(&mut command);
    command
}

fn configure_inherited_sidecar_fds(
    command: &mut Command,
    socket_fd: RawFd,
    session_token_fd: RawFd,
) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            command.pre_exec(move || {
                let socket_copy = libc::fcntl(socket_fd, libc::F_DUPFD, CHILD_DUPLICATE_FD_MIN);
                if socket_copy == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                let token_copy =
                    libc::fcntl(session_token_fd, libc::F_DUPFD, CHILD_DUPLICATE_FD_MIN);
                if token_copy == -1 {
                    libc::close(socket_copy);
                    return Err(std::io::Error::last_os_error());
                }
                let socket_result = libc::dup2(socket_copy, CHILD_SOCKET_FD);
                let socket_error = std::io::Error::last_os_error();
                let token_result = libc::dup2(token_copy, CHILD_SESSION_TOKEN_FD);
                let token_error = std::io::Error::last_os_error();
                libc::close(socket_copy);
                libc::close(token_copy);
                if socket_result == -1 {
                    return Err(socket_error);
                }
                if token_result == -1 {
                    return Err(token_error);
                }
                Ok(())
            });
        }
    }
}

fn configure_sidecar_session(command: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    Err(std::io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
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
            let owns_sidecar = {
                let mut state = lock_state(&self.state);
                if state.generation == self.generation {
                    state.starting = false;
                    state.running = false;
                }
                state.sidecar_owner_generation == Some(self.generation)
            };
            if owns_sidecar {
                schedule_startup_cleanup(self.state.clone(), self.generation);
            }
        }
    }
}

#[derive(Clone, Copy)]
struct SidecarIdentity {
    session_id: Option<i32>,
    process_group_id: Option<i32>,
}

struct VacantOwnerSlot<'a> {
    state: &'a mut AppState,
}

impl<'a> VacantOwnerSlot<'a> {
    fn new(state: &'a mut AppState) -> Option<Self> {
        if state.sidecar_owner_generation.is_some()
            || state.sidecar_child.is_some()
            || state.sidecar_session_id.is_some()
            || state.sidecar_process_group.is_some()
            || !state.sidecar_log_threads.is_empty()
        {
            None
        } else {
            Some(Self { state })
        }
    }

    fn retain_failed_child(
        &mut self,
        generation: u64,
        child: std::process::Child,
        identity: SidecarIdentity,
        message: &str,
    ) {
        self.state.sidecar_owner_generation = Some(generation);
        self.state.sidecar_child = Some(child);
        self.state.sidecar_session_id = identity.session_id;
        self.state.sidecar_process_group = identity.process_group_id;
        self.state.stopping_session_id = None;
        self.state.stopping_process_group = None;
        self.state.stopping = false;
        record_shutdown_failure_locked(self.state, message);
    }

    fn commit(self, generation: u64, guard: ChildGuard) {
        let (child, session_id, process_group_id, log_threads) = guard.take();
        self.state.sidecar_owner_generation = Some(generation);
        self.state.sidecar_child = Some(child);
        self.state.sidecar_session_id = session_id;
        self.state.sidecar_process_group = process_group_id;
        self.state.sidecar_log_threads = log_threads;
    }

    fn release(self) {}
}

struct ChildGuard {
    state: Arc<Mutex<AppState>>,
    generation: u64,
    child: Option<std::process::Child>,
    session_id: Option<i32>,
    process_group_id: Option<i32>,
    log_threads: Vec<JoinHandle<()>>,
}

impl ChildGuard {
    fn new(
        child: std::process::Child,
        state: Arc<Mutex<AppState>>,
        owner_slot: &mut VacantOwnerSlot<'_>,
        generation: u64,
    ) -> std::io::Result<Self> {
        Self::new_with(
            child,
            state,
            owner_slot,
            generation,
            sidecar_process_identity,
            terminate_managed_child,
        )
    }

    fn new_with<I, T>(
        mut child: std::process::Child,
        state: Arc<Mutex<AppState>>,
        owner_slot: &mut VacantOwnerSlot<'_>,
        generation: u64,
        resolve_identity: I,
        terminate: T,
    ) -> std::io::Result<Self>
    where
        I: FnOnce(&std::process::Child) -> std::io::Result<SidecarIdentity>,
        T: FnOnce(&mut std::process::Child, Option<i32>, Option<i32>) -> std::io::Result<()>,
    {
        let provisional = provisional_sidecar_identity(&child)?;
        let identity = match resolve_identity(&child) {
            Ok(identity) => identity,
            Err(error) => {
                let cleanup = terminate(
                    &mut child,
                    provisional.session_id,
                    provisional.process_group_id,
                );
                let Err(cleanup_error) = cleanup else {
                    return Err(error);
                };
                let message = format!(
                    "{error}; failed to clean provisional sidecar session: {cleanup_error}"
                );
                owner_slot.retain_failed_child(generation, child, provisional, &message);
                return Err(std::io::Error::other(message));
            }
        };
        Ok(Self {
            state,
            generation,
            child: Some(child),
            session_id: identity.session_id,
            process_group_id: identity.process_group_id,
            log_threads: Vec::new(),
        })
    }

    fn take(
        mut self,
    ) -> (
        std::process::Child,
        Option<i32>,
        Option<i32>,
        Vec<JoinHandle<()>>,
    ) {
        (
            self.child.take().expect("child guard must contain child"),
            self.session_id.take(),
            self.process_group_id.take(),
            std::mem::take(&mut self.log_threads),
        )
    }
}

fn retain_log_thread(state: &Arc<Mutex<AppState>>, generation: u64, thread: JoinHandle<()>) {
    let mut thread = Some(thread);
    let mut state = lock_state(state);
    if state.sidecar_owner_generation == Some(generation) {
        state
            .sidecar_log_threads
            .push(thread.take().expect("log thread must be available"));
    }
    drop(state);
    if let Some(thread) = thread {
        let _ = join_log_threads_bounded(vec![thread]);
    }
}

#[cfg(unix)]
fn provisional_sidecar_identity(child: &std::process::Child) -> std::io::Result<SidecarIdentity> {
    let pid = i32::try_from(child.id())
        .map_err(|_| std::io::Error::other("sidecar process id is out of range"))?;
    validate_session_id(pid)?;
    validate_process_group_id(pid)?;
    Ok(SidecarIdentity {
        session_id: Some(pid),
        process_group_id: Some(pid),
    })
}

#[cfg(not(unix))]
fn provisional_sidecar_identity(_child: &std::process::Child) -> std::io::Result<SidecarIdentity> {
    Ok(SidecarIdentity {
        session_id: None,
        process_group_id: None,
    })
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let termination = self
            .child
            .as_mut()
            .map(|child| terminate_managed_child(child, self.session_id, self.process_group_id));
        let still_alive = self
            .child
            .as_mut()
            .map(|child| {
                !managed_child_exited(child, self.session_id, self.process_group_id)
                    .unwrap_or(false)
            })
            .unwrap_or(false);
        if termination.is_some_and(|result| result.is_err()) && still_alive {
            let mut state = lock_state(&self.state);
            if state.sidecar_child.is_none() {
                state.sidecar_owner_generation = Some(self.generation);
                state.sidecar_child = self.child.take();
                state.sidecar_session_id = self.session_id.take();
                state.sidecar_process_group = self.process_group_id.take();
                state.sidecar_log_threads = std::mem::take(&mut self.log_threads);
                state.stopping = false;
                state.error = Some(crate::state::ServiceError {
                    category: "shutdown".into(),
                    message: "failed to clean up sidecar during startup".into(),
                });
                return;
            }
        }
        let _ = join_log_threads_bounded(std::mem::take(&mut self.log_threads));
    }
}

#[cfg(unix)]
fn sidecar_process_identity(child: &std::process::Child) -> std::io::Result<SidecarIdentity> {
    let pid = i32::try_from(child.id())
        .map_err(|_| std::io::Error::other("sidecar process id is out of range"))?;
    let session_id = unsafe { libc::getsid(pid) };
    if session_id == -1 {
        return Err(std::io::Error::last_os_error());
    }
    let process_group_id = unsafe { libc::getpgid(pid) };
    if process_group_id == -1 {
        return Err(std::io::Error::last_os_error());
    }
    let current_session = unsafe { libc::getsid(0) };
    let current_process_group = unsafe { libc::getpgrp() };
    if pid <= 1
        || session_id != pid
        || process_group_id != pid
        || session_id == current_session
        || process_group_id == current_process_group
    {
        return Err(std::io::Error::other(
            "sidecar did not start as an isolated session leader",
        ));
    }
    Ok(SidecarIdentity {
        session_id: Some(session_id),
        process_group_id: Some(process_group_id),
    })
}

#[cfg(not(unix))]
fn sidecar_process_identity(_child: &std::process::Child) -> std::io::Result<SidecarIdentity> {
    Ok(SidecarIdentity {
        session_id: None,
        process_group_id: None,
    })
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

#[cfg(target_os = "macos")]
fn terminate_managed_child(
    child: &mut std::process::Child,
    session_id: Option<i32>,
    process_group_id: Option<i32>,
) -> std::io::Result<()> {
    match (session_id, process_group_id) {
        (Some(session_id), Some(process_group_id)) => {
            terminate_macos_session(child, session_id, process_group_id, false)
        }
        (None, None) => terminate_child(child),
        _ => Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "incomplete sidecar session identity",
        )),
    }
}

#[cfg(not(target_os = "macos"))]
fn terminate_managed_child(
    child: &mut std::process::Child,
    _session_id: Option<i32>,
    process_group_id: Option<i32>,
) -> std::io::Result<()> {
    #[cfg(unix)]
    if let Some(process_group_id) = process_group_id {
        return terminate_process_group(child, process_group_id);
    }
    terminate_child(child)
}

fn managed_child_exited(
    child: &mut std::process::Child,
    session_id: Option<i32>,
    _process_group_id: Option<i32>,
) -> std::io::Result<bool> {
    let leader_exited = child_exited(child)?;
    #[cfg(target_os = "macos")]
    if let Some(session_id) = session_id {
        return Ok(leader_exited && macos_session_process_groups(session_id)?.is_empty());
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    if let Some(process_group_id) = _process_group_id {
        return Ok(leader_exited && !process_group_exists(process_group_id)?);
    }
    Ok(leader_exited)
}

#[cfg(all(unix, not(target_os = "macos")))]
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

#[cfg(all(unix, not(target_os = "macos")))]
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

#[cfg(target_os = "macos")]
#[link(name = "proc")]
extern "C" {
    fn proc_listallpids(buffer: *mut libc::c_void, buffersize: libc::c_int) -> libc::c_int;
}

#[cfg(target_os = "macos")]
fn macos_all_pids() -> std::io::Result<Vec<i32>> {
    let required = unsafe { proc_listallpids(std::ptr::null_mut(), 0) };
    if required <= 0 {
        return Err(std::io::Error::other("failed to enumerate processes"));
    }
    let mut capacity = usize::try_from(required)
        .unwrap_or(0)
        .saturating_add(64)
        .max(64);
    loop {
        let mut pids = vec![0_i32; capacity];
        let bytes = capacity
            .checked_mul(std::mem::size_of::<i32>())
            .and_then(|bytes| i32::try_from(bytes).ok())
            .ok_or_else(|| std::io::Error::other("process list is too large"))?;
        let count = unsafe { proc_listallpids(pids.as_mut_ptr().cast(), bytes) };
        if count < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let count = usize::try_from(count).unwrap_or(0);
        if count < capacity {
            pids.truncate(count);
            return Ok(pids);
        }
        capacity = capacity
            .checked_mul(2)
            .ok_or_else(|| std::io::Error::other("process list is too large"))?;
    }
}

#[cfg(target_os = "macos")]
fn validate_session_id(session_id: i32) -> std::io::Result<()> {
    let current_session = unsafe { libc::getsid(0) };
    if session_id <= 1 || session_id == current_session {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "refusing to signal an unsafe sidecar session",
        ));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn process_matches_session_group(pid: i32, session_id: i32, process_group_id: i32) -> bool {
    if pid <= 1 || unsafe { libc::getsid(pid) } != session_id {
        return false;
    }
    if unsafe { libc::getpgid(pid) } != process_group_id {
        return false;
    }
    (unsafe { libc::getsid(pid) }) == session_id
}

#[cfg(target_os = "macos")]
fn macos_session_process_groups(session_id: i32) -> std::io::Result<Vec<(i32, i32)>> {
    validate_session_id(session_id)?;
    let mut groups = std::collections::BTreeMap::new();
    for pid in macos_all_pids()? {
        if pid <= 1 || unsafe { libc::getsid(pid) } != session_id {
            continue;
        }
        let process_group_id = unsafe { libc::getpgid(pid) };
        if process_group_id <= 1 || unsafe { libc::getsid(pid) } != session_id {
            continue;
        }
        validate_process_group_id(process_group_id)?;
        groups.entry(process_group_id).or_insert(pid);
    }
    Ok(groups.into_iter().collect())
}

#[cfg(target_os = "macos")]
fn signal_macos_session_groups(
    session_id: i32,
    groups: &[(i32, i32)],
    signal: i32,
) -> std::io::Result<()> {
    for &(process_group_id, representative_pid) in groups {
        if process_matches_session_group(representative_pid, session_id, process_group_id) {
            signal_process_group(process_group_id, signal)?;
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn terminate_macos_session(
    child: &mut std::process::Child,
    session_id: i32,
    process_group_id: i32,
    force: bool,
) -> std::io::Result<()> {
    validate_session_id(session_id)?;
    validate_process_group_id(process_group_id)?;
    let signal = if force { libc::SIGKILL } else { libc::SIGTERM };
    let grace_deadline =
        std::time::Instant::now() + std::time::Duration::from_millis(TERMINATION_TIMEOUT_MS);
    while std::time::Instant::now() < grace_deadline {
        let groups = macos_session_process_groups(session_id)?;
        if child_exited(child)? && groups.is_empty() {
            return Ok(());
        }
        signal_macos_session_groups(session_id, &groups, signal)?;
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
    let kill_deadline =
        std::time::Instant::now() + std::time::Duration::from_millis(TERMINATION_TIMEOUT_MS);
    while std::time::Instant::now() < kill_deadline {
        let groups = macos_session_process_groups(session_id)?;
        if child_exited(child)? && groups.is_empty() {
            return Ok(());
        }
        signal_macos_session_groups(session_id, &groups, libc::SIGKILL)?;
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
    Err(std::io::Error::new(
        std::io::ErrorKind::TimedOut,
        "sidecar session did not terminate",
    ))
}

struct LogThreadJoinOutcome {
    detached: usize,
    error: Option<String>,
}

fn join_log_threads_bounded(threads: Vec<JoinHandle<()>>) -> LogThreadJoinOutcome {
    let current_thread = std::thread::current().id();
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_millis(LOG_THREAD_JOIN_TIMEOUT_MS);
    while std::time::Instant::now() < deadline
        && threads
            .iter()
            .any(|thread| thread.thread().id() != current_thread && !thread.is_finished())
    {
        std::thread::sleep(std::time::Duration::from_millis(10));
    }

    let mut detached = 0;
    let mut panicked = false;
    for thread in threads {
        if thread.thread().id() == current_thread || !thread.is_finished() {
            detached += 1;
        } else {
            panicked |= thread.join().is_err();
        }
    }
    let error = if panicked {
        Some("sidecar log reader stopped unexpectedly".into())
    } else {
        None
    };
    LogThreadJoinOutcome { detached, error }
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

fn request_graceful_shutdown(port: u16, token: &str) -> std::io::Result<()> {
    if port == 0
        || token.len() < 32
        || !token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._~-".contains(&byte))
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "invalid sidecar shutdown endpoint identity",
        ));
    }

    let timeout = std::time::Duration::from_millis(SHUTDOWN_REQUEST_TIMEOUT_MS);
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let mut stream = TcpStream::connect_timeout(&address, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    write!(
        stream,
        "POST /shutdown HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{SESSION_HEADER}: {token}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    )?;
    stream.flush()?;

    let mut response = Vec::with_capacity(256);
    std::io::Read::by_ref(&mut stream)
        .take((MAX_SHUTDOWN_RESPONSE_BYTES + 1) as u64)
        .read_to_end(&mut response)?;
    if response.len() > MAX_SHUTDOWN_RESPONSE_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "sidecar shutdown response exceeded the limit",
        ));
    }
    let Some(header_end) = response.windows(4).position(|window| window == b"\r\n\r\n") else {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "invalid sidecar shutdown response",
        ));
    };
    let headers = std::str::from_utf8(&response[..header_end]).map_err(|_| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "invalid sidecar shutdown response",
        )
    })?;
    let status = headers.lines().next().unwrap_or_default();
    if !matches!(status, "HTTP/1.1 200 OK" | "HTTP/1.0 200 OK") {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "sidecar rejected graceful shutdown",
        ));
    }
    let body = &response[header_end + 4..];
    let payload: serde_json::Value = serde_json::from_slice(body).map_err(|_| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "invalid sidecar shutdown acknowledgement",
        )
    })?;
    if payload != serde_json::json!({"status": "shutting_down"}) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "invalid sidecar shutdown acknowledgement",
        ));
    }
    Ok(())
}

fn wait_for_managed_child_exit(
    child: &mut std::process::Child,
    session_id: Option<i32>,
    process_group_id: Option<i32>,
    timeout: std::time::Duration,
) -> std::io::Result<bool> {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if managed_child_exited(child, session_id, process_group_id)? {
            return Ok(true);
        }
        if std::time::Instant::now() >= deadline {
            return Ok(false);
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
}

fn gracefully_terminate_managed_child(
    child: &mut std::process::Child,
    port: u16,
    token: &str,
    session_id: Option<i32>,
    process_group_id: Option<i32>,
) -> std::io::Result<()> {
    if request_graceful_shutdown(port, token).is_ok()
        && matches!(
            wait_for_managed_child_exit(
                child,
                session_id,
                process_group_id,
                std::time::Duration::from_millis(SHUTDOWN_EXIT_TIMEOUT_MS),
            ),
            Ok(true)
        )
    {
        return Ok(());
    }
    terminate_managed_child(child, session_id, process_group_id)
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
    let state = lock_state(state);
    state.generation == generation && !state.application_exiting
}

fn failed_or_superseded(
    state: &Arc<Mutex<AppState>>,
    generation: u64,
    message: impl Into<String>,
) -> SidecarStartError {
    let state = lock_state(state);
    if state.generation != generation || state.application_exiting {
        return SidecarStartError::Superseded;
    }
    let message = state
        .error
        .as_ref()
        .filter(|error| matches!(error.category.as_str(), "logging" | "shutdown"))
        .map(|error| error.message.clone())
        .unwrap_or_else(|| message.into());
    SidecarStartError::Failed {
        message,
        generation,
    }
}

fn startup_child_exited(
    state: &Arc<Mutex<AppState>>,
    generation: u64,
) -> Result<bool, SidecarStartError> {
    let mut state = lock_state(state);
    if state.generation == generation
        && state
            .error
            .as_ref()
            .is_some_and(|error| error.category == "logging")
    {
        return Err(SidecarStartError::Failed {
            message: state
                .error
                .as_ref()
                .map(|error| error.message.clone())
                .unwrap_or_else(|| "sidecar logging failed".into()),
            generation,
        });
    }
    if state.generation != generation
        || state.application_exiting
        || state.sidecar_owner_generation != Some(generation)
    {
        return Err(SidecarStartError::Superseded);
    }
    let child = state
        .sidecar_child
        .as_mut()
        .ok_or(SidecarStartError::Superseded)?;
    child_exited(child).map_err(|error| SidecarStartError::Failed {
        message: error.to_string(),
        generation,
    })
}

async fn start_sidecar_core(
    state: Arc<Mutex<AppState>>,
    runtime: SidecarRuntime,
    restore_desired_running: bool,
) -> Result<(), SidecarStartError> {
    let (listener, port, session_token, generation) = {
        let mut s = lock_state(&state);
        if s.application_exiting {
            return Err(SidecarStartError::Superseded);
        }
        if s.running
            || s.starting
            || s.stopping
            || s.sidecar_child.is_some()
            || s.sidecar_session_id.is_some()
            || s.sidecar_process_group.is_some()
            || s.sidecar_owner_generation.is_some()
            || !s.sidecar_log_threads.is_empty()
        {
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
    make_socket_close_on_exec(&listener).map_err(|error| {
        failed_or_superseded(
            &state,
            generation,
            format!("failed to inherit sidecar socket: {error}"),
        )
    })?;

    let parent_pid = std::process::id();
    {
        let mut s = lock_state(&state);
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
    let session_token_reader = session_token_channel(&session_token).map_err(|error| {
        failed_or_superseded(
            &state,
            generation,
            format!("failed to create sidecar session channel: {error}"),
        )
    })?;
    let mut command = sidecar_command(
        &runtime.script,
        listener.as_raw_fd(),
        session_token_reader.as_raw_fd(),
        parent_pid,
    );
    command.envs(runtime.extra_env.iter().cloned());
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    {
        let mut s = lock_state(&state);
        if s.generation != generation
            || s.application_exiting
            || !s.desired_running
            || s.manual_stopped
        {
            return Err(SidecarStartError::Superseded);
        }
        let mut owner_slot =
            VacantOwnerSlot::new(&mut s).ok_or(SidecarStartError::AlreadyActive)?;
        let child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                let message = format!("failed to start {}: {error}", runtime.script.display());
                owner_slot.release();
                drop(s);
                return Err(failed_or_superseded(&state, generation, message));
            }
        };
        let child_guard = match ChildGuard::new(child, state.clone(), &mut owner_slot, generation) {
            Ok(child_guard) => child_guard,
            Err(error) => {
                let message = format!("failed to isolate sidecar process: {error}");
                owner_slot.release();
                drop(s);
                return Err(failed_or_superseded(&state, generation, message));
            }
        };
        owner_slot.commit(generation, child_guard);
    }
    drop(session_token_reader);
    let (child_stdout, child_stderr) = {
        let mut s = lock_state(&state);
        if s.generation != generation
            || s.application_exiting
            || s.sidecar_owner_generation != Some(generation)
        {
            return Err(SidecarStartError::Superseded);
        }
        let child = s
            .sidecar_child
            .as_mut()
            .ok_or(SidecarStartError::Superseded)?;
        (child.stdout.take(), child.stderr.take())
    };
    let child_stdout = child_stdout
        .ok_or_else(|| failed_or_superseded(&state, generation, "sidecar stdout unavailable"))?;
    let child_stderr = child_stderr
        .ok_or_else(|| failed_or_superseded(&state, generation, "sidecar stderr unavailable"))?;
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
    retain_log_thread(&state, generation, stdout_thread);
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
    retain_log_thread(&state, generation, stderr_thread);

    let ready_deadline = tokio::time::Instant::now() + runtime.startup_timeout;
    let mut ready_rx = std::pin::pin!(ready_rx);
    let ready_line = loop {
        if !generation_is_current(&state, generation) {
            return Err(SidecarStartError::Superseded);
        }
        if startup_child_exited(&state, generation)? {
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
        if startup_child_exited(&state, generation)? {
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
        let mut s = lock_state(&state);
        if s.generation != generation
            || s.application_exiting
            || !s.desired_running
            || s.manual_stopped
        {
            return Err(SidecarStartError::Superseded);
        }
        if let Some(error) = s.error.as_ref().filter(|error| error.category == "logging") {
            return Err(SidecarStartError::Failed {
                message: error.message.clone(),
                generation,
            });
        }
        if s.sidecar_owner_generation != Some(generation) || s.sidecar_child.is_none() {
            return Err(SidecarStartError::Superseded);
        }
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
    if lock_state(&state).application_exiting {
        return Err("application is exiting".into());
    }
    let runtime = match runtime {
        Ok(runtime) => runtime,
        Err(message) => {
            let sanitized = sanitize_message(&message, &lock_state(&state).session_token);
            if action == SidecarAction::Restart {
                stop_sidecar_async(state.clone(), StopReason::Restart).await?;
            }
            let generation = lock_state(&state).generation;
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
    if state.application_exiting {
        return;
    }
    state.desired_running = true;
    state.manual_stopped = false;
    state.service_state = "restarting".into();
    state.error = None;
    state.health_failures = 0;
}

pub fn health_failure_requires_restart(state: &mut AppState) -> bool {
    if state.application_exiting || !state.desired_running || state.manual_stopped {
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

struct OwnedSidecar {
    owner_generation: Option<u64>,
    child: Option<std::process::Child>,
    session_id: Option<i32>,
    process_group_id: Option<i32>,
    log_threads: Vec<JoinHandle<()>>,
    port: u16,
    session_token: String,
}

impl OwnedSidecar {
    fn take(state: &mut AppState) -> Self {
        Self {
            owner_generation: state.sidecar_owner_generation.take(),
            child: state.sidecar_child.take(),
            session_id: state.sidecar_session_id.take(),
            process_group_id: state.sidecar_process_group.take(),
            log_threads: std::mem::take(&mut state.sidecar_log_threads),
            port: state.port,
            session_token: state.session_token.clone(),
        }
    }

    fn is_empty(&self) -> bool {
        self.owner_generation.is_none()
            && self.child.is_none()
            && self.session_id.is_none()
            && self.process_group_id.is_none()
            && self.log_threads.is_empty()
    }
}

struct OwnerLease {
    state: Arc<Mutex<AppState>>,
    owner: Option<OwnedSidecar>,
    completion: Arc<std::sync::Condvar>,
}

impl OwnerLease {
    fn owner_mut(&mut self) -> &mut OwnedSidecar {
        self.owner.as_mut().expect("owner lease must be active")
    }
}

impl Drop for OwnerLease {
    fn drop(&mut self) {
        let Some(mut owner) = self.owner.take() else {
            return;
        };
        let mut state = lock_state(&self.state);
        if state.sidecar_child.is_none()
            && state.sidecar_session_id.is_none()
            && state.sidecar_process_group.is_none()
            && state.sidecar_log_threads.is_empty()
        {
            state.sidecar_owner_generation = owner.owner_generation.take();
            state.sidecar_child = owner.child.take();
            state.sidecar_session_id = owner.session_id.take();
            state.sidecar_process_group = owner.process_group_id.take();
            state.sidecar_log_threads = std::mem::take(&mut owner.log_threads);
        }
        state.stopping_session_id = None;
        state.stopping_process_group = None;
        state.stopping = false;
        state.logging_cleanup_scheduled = false;
        drop(state);
        self.completion.notify_all();
    }
}

fn apply_stop_intent(state: &mut AppState, reason: StopReason) {
    match reason {
        StopReason::Manual => {
            state.desired_running = false;
            state.manual_stopped = true;
            state.exit_stop_requested = false;
        }
        StopReason::Exit => {
            state.application_exiting = true;
            state.desired_running = false;
            if !state.manual_stopped {
                state.exit_stop_requested = true;
            }
        }
        StopReason::Restart | StopReason::LoggingFailure | StopReason::StartupFailure => {}
    }
}

fn finish_empty_stop(state: &mut AppState, reason: StopReason) {
    let reason = if state.manual_stopped {
        StopReason::Manual
    } else if state.exit_stop_requested {
        StopReason::Exit
    } else {
        reason
    };
    state.stopping_session_id = None;
    state.stopping_process_group = None;
    state.stopping = false;
    state.exit_stop_requested = false;
    state.logging_cleanup_scheduled = false;
    state.running = false;
    state.starting = false;
    state.health_failures = 0;
    state.session_token.clear();
    if state.manual_stopped {
        state.service_state = "failed".into();
        state.error = Some(crate::state::ServiceError {
            category: "manual_stop".into(),
            message: "service stopped by user".into(),
        });
    } else {
        match reason {
            StopReason::Restart => state.service_state = "restarting".into(),
            StopReason::Exit => state.push_log("[sidecar] application exit"),
            StopReason::LoggingFailure | StopReason::StartupFailure => {}
            StopReason::Manual => unreachable!("manual intent must be handled above"),
        }
    }
    state.stop_completed.notify_all();
}

#[cfg(target_os = "macos")]
fn force_terminate_managed_child(
    child: &mut std::process::Child,
    session_id: Option<i32>,
    _process_group_id: Option<i32>,
) -> std::io::Result<()> {
    let session_id = session_id.ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "missing sidecar session identity",
        )
    })?;
    validate_session_id(session_id)?;
    let process_group_id = if process_matches_session_group(child.id() as i32, session_id, unsafe {
        libc::getpgid(child.id() as i32)
    }) {
        unsafe { libc::getpgid(child.id() as i32) }
    } else {
        macos_session_process_groups(session_id)?
            .first()
            .map(|(process_group_id, _)| *process_group_id)
            .ok_or_else(|| std::io::Error::other("sidecar session identity is no longer live"))?
    };
    terminate_macos_session(child, session_id, process_group_id, true)
}

#[cfg(not(target_os = "macos"))]
fn force_terminate_managed_child(
    child: &mut std::process::Child,
    session_id: Option<i32>,
    process_group_id: Option<i32>,
) -> std::io::Result<()> {
    terminate_managed_child(child, session_id, process_group_id)
}

#[cfg(target_os = "macos")]
fn force_terminate_stopping_identity(
    session_id: Option<i32>,
    process_group_id: Option<i32>,
) -> std::io::Result<()> {
    let session_id = session_id.ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "missing stopping sidecar session identity",
        )
    })?;
    let process_group_id = process_group_id.ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "missing stopping sidecar process group identity",
        )
    })?;
    validate_session_id(session_id)?;
    validate_process_group_id(process_group_id)?;

    let deadline =
        std::time::Instant::now() + std::time::Duration::from_millis(EXIT_STOP_FORCE_TIMEOUT_MS);
    loop {
        let groups = macos_session_process_groups(session_id)?;
        if groups.is_empty() {
            return Ok(());
        }
        signal_macos_session_groups(session_id, &groups, libc::SIGKILL)?;
        if std::time::Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
    if macos_session_process_groups(session_id)?.is_empty() {
        Ok(())
    } else {
        Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "stopping sidecar session did not terminate",
        ))
    }
}

#[cfg(all(unix, not(target_os = "macos")))]
fn force_terminate_stopping_identity(
    _session_id: Option<i32>,
    process_group_id: Option<i32>,
) -> std::io::Result<()> {
    let process_group_id = process_group_id.ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "missing stopping sidecar process group identity",
        )
    })?;
    validate_process_group_id(process_group_id)?;
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_millis(EXIT_STOP_FORCE_TIMEOUT_MS);
    loop {
        if !process_group_exists(process_group_id)? {
            return Ok(());
        }
        signal_process_group(process_group_id, libc::SIGKILL)?;
        if std::time::Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
    if process_group_exists(process_group_id)? {
        Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "stopping sidecar process group did not terminate",
        ))
    } else {
        Ok(())
    }
}

#[cfg(not(unix))]
fn force_terminate_stopping_identity(
    _session_id: Option<i32>,
    _process_group_id: Option<i32>,
) -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "cannot force a concurrent sidecar stop on this platform",
    ))
}

fn wait_for_stop_completion(
    state: &Arc<Mutex<AppState>>,
    timeout: std::time::Duration,
) -> Result<bool, String> {
    let deadline = std::time::Instant::now() + timeout;
    let mut state_guard = lock_state(state);
    let completion = state_guard.stop_completed.clone();
    while state_guard.stopping {
        let now = std::time::Instant::now();
        if now >= deadline {
            return Ok(false);
        }
        let remaining = deadline.saturating_duration_since(now);
        let (next, wait) = match completion.wait_timeout(state_guard, remaining) {
            Ok(result) => result,
            Err(error) => error.into_inner(),
        };
        state_guard = next;
        if wait.timed_out() && state_guard.stopping {
            return Ok(false);
        }
    }
    Ok(true)
}

fn concurrent_exit_stop_finished(state: &AppState) -> bool {
    !state.stopping
        && !state.starting
        && !state.running
        && state.stopping_session_id.is_none()
        && state.stopping_process_group.is_none()
        && state.sidecar_owner_generation.is_none()
        && state.sidecar_child.is_none()
        && state.sidecar_session_id.is_none()
        && state.sidecar_process_group.is_none()
        && state.sidecar_log_threads.is_empty()
        && state.session_token.is_empty()
}

fn resolve_concurrent_exit_stop(state: &Arc<Mutex<AppState>>, force: bool) -> Result<(), String> {
    if force {
        let (stopping, session_id, process_group_id) = {
            let state = lock_state(state);
            (
                state.stopping,
                state.stopping_session_id,
                state.stopping_process_group,
            )
        };
        if stopping {
            let kill_error = force_terminate_stopping_identity(session_id, process_group_id)
                .err()
                .map(|error| format!("failed to kill concurrent sidecar session: {error}"));
            let released = wait_for_stop_completion(
                state,
                std::time::Duration::from_millis(EXIT_STOP_RELEASE_TIMEOUT_MS),
            )?;
            if !released {
                let release_error = "sidecar stop owner did not release after final session kill";
                return Err(match kill_error {
                    Some(kill_error) => format!("{kill_error}; {release_error}"),
                    None => release_error.into(),
                });
            }
        }
    } else if !wait_for_stop_completion(
        state,
        std::time::Duration::from_millis(EXIT_STOP_WAIT_TIMEOUT_MS),
    )? {
        return Err("timed out waiting for concurrent sidecar shutdown".into());
    }

    let state = lock_state(state);
    if concurrent_exit_stop_finished(&state) {
        Ok(())
    } else {
        Err("concurrent sidecar shutdown retained managed process state".into())
    }
}

fn stop_sidecar_internal(
    state: Arc<Mutex<AppState>>,
    reason: StopReason,
    force: bool,
) -> Result<(), String> {
    let mut lease = {
        let mut state_guard = lock_state(&state);
        apply_stop_intent(&mut state_guard, reason);
        if state_guard.stopping {
            if reason == StopReason::Manual {
                state_guard.generation = state_guard.generation.wrapping_add(1).max(1);
                drop(state_guard);
                let completed = wait_for_stop_completion(
                    &state,
                    std::time::Duration::from_millis(MANUAL_STOP_WAIT_TIMEOUT_MS),
                )?;
                if !completed {
                    return Err(
                        "timed out waiting for concurrent sidecar shutdown after manual stop"
                            .into(),
                    );
                }
                let state = lock_state(&state);
                return if concurrent_exit_stop_finished(&state) {
                    Ok(())
                } else {
                    Err("concurrent manual sidecar shutdown retained managed process state".into())
                };
            }
            if reason == StopReason::Exit {
                state_guard.generation = state_guard.generation.wrapping_add(1).max(1);
                drop(state_guard);
                return resolve_concurrent_exit_stop(&state, force);
            }
            if matches!(
                reason,
                StopReason::LoggingFailure | StopReason::StartupFailure
            ) {
                return Ok(());
            }
            return Err("sidecar shutdown already in progress".into());
        }
        state_guard.stopping = true;
        if matches!(
            reason,
            StopReason::Manual | StopReason::Restart | StopReason::Exit
        ) {
            state_guard.generation = state_guard.generation.wrapping_add(1).max(1);
        }
        state_guard.starting = false;
        state_guard.health_failures = 0;
        let owner = OwnedSidecar::take(&mut state_guard);
        state_guard.stopping_session_id = owner.session_id;
        state_guard.stopping_process_group = owner.process_group_id;
        if owner.is_empty() {
            finish_empty_stop(&mut state_guard, reason);
            return Ok(());
        }
        let completion = state_guard.stop_completed.clone();
        drop(state_guard);
        OwnerLease {
            state: state.clone(),
            owner: Some(owner),
            completion,
        }
    };

    let had_child = lease.owner_mut().child.is_some();
    let termination_error = {
        let owner = lease.owner_mut();
        match owner.child.as_mut() {
            Some(child) => {
                let result = if force {
                    force_terminate_managed_child(child, owner.session_id, owner.process_group_id)
                } else {
                    gracefully_terminate_managed_child(
                        child,
                        owner.port,
                        &owner.session_token,
                        owner.session_id,
                        owner.process_group_id,
                    )
                };
                result
                    .err()
                    .map(|error| format!("failed to stop sidecar session: {error}"))
            }
            None => None,
        }
    };

    if let Some(message) = termination_error.as_ref() {
        let process_still_alive = {
            let owner = lease.owner_mut();
            owner
                .child
                .as_mut()
                .map(|child| {
                    !managed_child_exited(child, owner.session_id, owner.process_group_id)
                        .unwrap_or(false)
                })
                .unwrap_or(false)
        };
        if process_still_alive {
            record_shutdown_failure(&state, message);
            return Err(message.clone());
        }
    }

    let readers = {
        let owner = lease.owner_mut();
        join_log_threads_bounded(std::mem::take(&mut owner.log_threads))
    };
    let mut current = lock_state(&state);
    let finished_owner = lease.owner.take();
    current.sidecar_owner_generation = None;
    current.sidecar_child = None;
    current.sidecar_session_id = None;
    current.sidecar_process_group = None;
    current.sidecar_log_threads.clear();
    finish_empty_stop(&mut current, reason);
    if had_child {
        current.push_log("[sidecar] stopped");
    }
    if readers.detached > 0 {
        current.push_log(format!(
            "[sidecar] detached {} stale log reader(s) after process exit",
            readers.detached
        ));
    }
    let cleanup_error = termination_error.or(readers.error);
    if let Some(message) = cleanup_error.as_ref() {
        current.service_state = "failed".into();
        current.error = Some(crate::state::ServiceError {
            category: "shutdown".into(),
            message: message.clone(),
        });
        current.push_log(format!("[sidecar] shutdown failure: {message}"));
    }
    drop(current);
    drop(finished_owner);
    drop(lease);
    cleanup_error.map_or(Ok(()), Err)
}

pub fn stop_sidecar(state: Arc<Mutex<AppState>>, reason: StopReason) -> Result<(), String> {
    stop_sidecar_internal(state, reason, false)
}

type CleanupTask = Box<dyn FnOnce() + Send + 'static>;

fn spawn_cleanup_thread(name: &'static str, task: CleanupTask) -> Result<(), CleanupTask> {
    let slot = Arc::new(Mutex::new(Some(task)));
    let worker_slot = slot.clone();
    let spawn = std::thread::Builder::new()
        .name(name.into())
        .spawn(move || {
            let task = match worker_slot.lock() {
                Ok(mut slot) => slot.take(),
                Err(error) => error.into_inner().take(),
            };
            if let Some(task) = task {
                task();
            }
        });
    match spawn {
        Ok(_) => Ok(()),
        Err(_) => {
            let task = match slot.lock() {
                Ok(mut slot) => slot.take(),
                Err(error) => error.into_inner().take(),
            }
            .expect("failed cleanup thread must leave its task available");
            Err(task)
        }
    }
}

fn schedule_logging_cleanup_with<F>(state: Arc<Mutex<AppState>>, spawn: F)
where
    F: FnOnce(&'static str, CleanupTask) -> Result<(), CleanupTask>,
{
    let cleanup_state = state.clone();
    let task: CleanupTask = Box::new(move || {
        if let Err(error) =
            stop_sidecar_internal(cleanup_state.clone(), StopReason::LoggingFailure, false)
        {
            lock_state(&cleanup_state)
                .push_log(format!("[sidecar] logging cleanup failed: {error}"));
        }
    });
    if let Err(task) = spawn("sidecar-logging-cleanup", task) {
        lock_state(&state)
            .push_log("[sidecar] logging cleanup thread unavailable; running synchronously");
        task();
    }
}

fn schedule_startup_cleanup_with<F>(state: Arc<Mutex<AppState>>, generation: u64, spawn: F)
where
    F: FnOnce(&'static str, CleanupTask) -> Result<(), CleanupTask>,
{
    let cleanup_state = state.clone();
    let task: CleanupTask = Box::new(move || {
        let owns_generation =
            lock_state(&cleanup_state).sidecar_owner_generation == Some(generation);
        if owns_generation {
            if let Err(error) =
                stop_sidecar_internal(cleanup_state, StopReason::StartupFailure, false)
            {
                eprintln!("sidecar startup cleanup failed: {error}");
            }
        }
    });
    if let Err(task) = spawn("sidecar-startup-cleanup", task) {
        lock_state(&state)
            .push_log("[sidecar] startup cleanup thread unavailable; running synchronously");
        task();
    }
}

fn schedule_logging_cleanup(state: Arc<Mutex<AppState>>) {
    schedule_logging_cleanup_with(state, spawn_cleanup_thread);
}

fn schedule_startup_cleanup(state: Arc<Mutex<AppState>>, generation: u64) {
    schedule_startup_cleanup_with(state, generation, spawn_cleanup_thread);
}

pub fn shutdown_sidecar_for_exit(state: Arc<Mutex<AppState>>) -> Result<(), String> {
    {
        let mut state_guard = lock_state(&state);
        state_guard.application_exiting = true;
        state_guard.desired_running = false;
    }
    let cleanup = match stop_sidecar_internal(state.clone(), StopReason::Exit, false) {
        Ok(()) => Ok(()),
        Err(graceful_error) => stop_sidecar_internal(state.clone(), StopReason::Exit, true)
            .map_err(|force_error| {
                format!("{graceful_error}; final sidecar session kill failed: {force_error}")
            }),
    };
    if let Err(error) = cleanup {
        let error = record_shutdown_failure(&state, &error);
        return Err(error);
    }
    let state = lock_state(&state);
    if concurrent_exit_stop_finished(&state) && state.session_token.is_empty() {
        Ok(())
    } else {
        Err("sidecar exit cleanup returned with managed session state".into())
    }
}

pub async fn stop_sidecar_async(
    state: Arc<Mutex<AppState>>,
    reason: StopReason,
) -> Result<(), String> {
    tokio::task::spawn_blocking(move || stop_sidecar(state, reason))
        .await
        .map_err(|error| format!("sidecar shutdown task failed: {error}"))?
}

async fn restart_sidecar_core(
    state: Arc<Mutex<AppState>>,
    runtime: SidecarRuntime,
) -> Result<(), SidecarStartError> {
    if lock_state(&state).application_exiting {
        return Err(SidecarStartError::Superseded);
    }
    stop_sidecar_async(state.clone(), StopReason::Restart)
        .await
        .map_err(|message| {
            let generation = lock_state(&state).generation;
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
        let mut s = lock_state(&state);
        prepare_retry(&mut s);
    }
    restart_sidecar(app, state).await
}

pub async fn health_loop(app: tauri::AppHandle, state: Arc<Mutex<AppState>>) {
    let client = match health_client() {
        Ok(client) => client,
        Err(error) => {
            let generation = lock_state(&state).generation;
            record_failure(&state, generation, "configuration", error);
            return;
        }
    };
    tokio::time::sleep(std::time::Duration::from_secs(HEALTH_STARTUP_GRACE_SECS)).await;
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(HEALTH_INTERVAL_SECS)).await;
        let (port, token, generation) = {
            let s = lock_state(&state);
            if !s.desired_running || s.manual_stopped || !s.running {
                continue;
            }
            (s.port, s.session_token.clone(), s.generation)
        };
        match instance_is_healthy(client, port, &token).await {
            true => {
                let mut state = lock_state(&state);
                if state.generation == generation {
                    state.health_failures = 0;
                }
            }
            false => {
                let should_restart = {
                    let mut s = lock_state(&state);
                    s.generation == generation && health_failure_requires_restart(&mut s)
                };
                if should_restart {
                    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                    let superseded = {
                        let state = lock_state(&state);
                        state.generation != generation || state.application_exiting
                    };
                    if superseded {
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
