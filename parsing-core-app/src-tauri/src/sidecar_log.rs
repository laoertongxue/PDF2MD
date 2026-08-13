use std::fs::{create_dir_all, File, OpenOptions};
use std::io::{BufRead, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

pub(crate) const MAX_READY_LINE_BYTES: usize = 4096;
pub(crate) const MAX_LOG_BYTES: u64 = 5 * 1024 * 1024;
const LOG_READ_BUFFER_BYTES: usize = 4096;
const REDACTED_SECRET: &[u8] = b"[REDACTED]";

#[derive(Clone)]
pub(crate) struct SharedLog(Arc<Mutex<RotatingLog>>);

impl SharedLog {
    pub(crate) fn new(log: RotatingLog) -> Self {
        Self(Arc::new(Mutex::new(log)))
    }
}

pub(crate) struct RotatingLog {
    path: PathBuf,
    file: File,
    size: u64,
    max_bytes: u64,
}

impl RotatingLog {
    fn rotate(&mut self) -> std::io::Result<()> {
        self.file.flush()?;
        let backup = backup_path(&self.path);
        remove_existing_log_file(&backup)?;
        std::fs::rename(&self.path, &backup)?;
        secure_existing_log_file(&backup, self.max_bytes)?;
        self.file = open_private_file(&self.path, true, false)?;
        self.size = 0;
        Ok(())
    }
}

impl Write for RotatingLog {
    fn write(&mut self, mut buffer: &[u8]) -> std::io::Result<usize> {
        let total = buffer.len();
        while !buffer.is_empty() {
            if self.size >= self.max_bytes {
                self.rotate()?;
            }
            let available = usize::try_from(self.max_bytes - self.size)
                .unwrap_or(usize::MAX)
                .min(buffer.len());
            self.file.write_all(&buffer[..available])?;
            self.size += u64::try_from(available).unwrap_or(u64::MAX);
            buffer = &buffer[available..];
        }
        Ok(total)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.file.flush()
    }
}

pub(crate) fn prepare_private_log_directory(path: &Path) -> std::io::Result<()> {
    match std::fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            return Err(invalid_log_target());
        }
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => create_dir_all(path)?,
        Err(error) => return Err(error),
    }
    let metadata = std::fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(invalid_log_target());
    }
    set_private_directory_permissions(path)
}

pub(crate) fn open_rotating_log(path: &Path, max_bytes: u64) -> std::io::Result<RotatingLog> {
    if max_bytes == 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "log size limit must be positive",
        ));
    }
    reject_non_regular_target(path)?;
    let backup = backup_path(path);
    reject_non_regular_target(&backup)?;
    if backup.exists() {
        secure_existing_log_file(&backup, max_bytes)?;
    }

    let file = open_private_file(path, false, true)?;
    let mut log = RotatingLog {
        path: path.to_path_buf(),
        size: file.metadata()?.len(),
        file,
        max_bytes,
    };
    if log.size > log.max_bytes {
        log.rotate()?;
    }
    Ok(log)
}

fn backup_path(path: &Path) -> PathBuf {
    PathBuf::from(format!("{}.1", path.display()))
}

fn reject_non_regular_target(path: &Path) -> std::io::Result<()> {
    match std::fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            Err(invalid_log_target())
        }
        Ok(_) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn remove_existing_log_file(path: &Path) -> std::io::Result<()> {
    match std::fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            Err(invalid_log_target())
        }
        Ok(_) => std::fs::remove_file(path),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn secure_existing_log_file(path: &Path, max_bytes: u64) -> std::io::Result<()> {
    reject_non_regular_target(path)?;
    let file = open_private_file(path, false, false)?;
    if file.metadata()?.len() > max_bytes {
        file.set_len(max_bytes)?;
    }
    Ok(())
}

fn open_private_file(path: &Path, truncate: bool, append: bool) -> std::io::Result<File> {
    let mut options = OpenOptions::new();
    options
        .create(true)
        .write(true)
        .truncate(truncate)
        .append(append);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    let file = options.open(path)?;
    set_private_file_permissions(&file)?;
    Ok(file)
}

#[cfg(unix)]
fn set_private_directory_permissions(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
}

#[cfg(not(unix))]
fn set_private_directory_permissions(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn set_private_file_permissions(file: &File) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    file.set_permissions(std::fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn set_private_file_permissions(_file: &File) -> std::io::Result<()> {
    Ok(())
}

fn invalid_log_target() -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::InvalidInput,
        "sidecar log target must be a regular private file",
    )
}

impl Write for SharedLog {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        self.0
            .lock()
            .map_err(|_| std::io::Error::other("sidecar log lock poisoned"))?
            .write(buffer)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.0
            .lock()
            .map_err(|_| std::io::Error::other("sidecar log lock poisoned"))?
            .flush()
    }
}

struct StreamingRedactor<'a, W: Write> {
    writer: &'a mut W,
    secret: &'a [u8],
    pending: Vec<u8>,
}

impl<'a, W: Write> StreamingRedactor<'a, W> {
    fn new(writer: &'a mut W, secret: &'a [u8]) -> Self {
        Self {
            writer,
            secret,
            pending: Vec::with_capacity(LOG_READ_BUFFER_BYTES + secret.len()),
        }
    }

    fn write_chunk(&mut self, chunk: &[u8]) -> std::io::Result<()> {
        self.pending.extend_from_slice(chunk);
        self.flush_pending(false)
    }

    fn finish(mut self) -> std::io::Result<()> {
        self.flush_pending(true)?;
        self.writer.flush()
    }

    fn flush_pending(&mut self, finish: bool) -> std::io::Result<()> {
        if self.secret.is_empty() {
            self.writer.write_all(&self.pending)?;
            self.pending.clear();
            return Ok(());
        }

        let mut output = Vec::with_capacity(self.pending.len());
        let mut consumed = 0;
        while let Some(offset) = find_bytes(&self.pending[consumed..], self.secret) {
            let match_start = consumed + offset;
            output.extend_from_slice(&self.pending[consumed..match_start]);
            output.extend_from_slice(REDACTED_SECRET);
            consumed = match_start + self.secret.len();
        }

        let remaining = &self.pending[consumed..];
        let retained = if finish {
            0
        } else {
            matching_secret_prefix_suffix(remaining, self.secret)
        };
        let emit_length = remaining.len() - retained;
        output.extend_from_slice(&remaining[..emit_length]);
        self.writer.write_all(&output)?;

        let retained_bytes = remaining[emit_length..].to_vec();
        self.pending.clear();
        self.pending.extend_from_slice(&retained_bytes);
        Ok(())
    }
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|candidate| candidate == needle)
}

fn matching_secret_prefix_suffix(buffer: &[u8], secret: &[u8]) -> usize {
    let maximum = buffer.len().min(secret.len().saturating_sub(1));
    (1..=maximum)
        .rev()
        .find(|length| buffer.ends_with(&secret[..*length]))
        .unwrap_or(0)
}

pub(crate) fn copy_redacted<R: Read, W: Write>(
    reader: &mut R,
    writer: &mut W,
    secret: &[u8],
) -> std::io::Result<()> {
    let mut redactor = StreamingRedactor::new(writer, secret);
    let mut buffer = [0_u8; LOG_READ_BUFFER_BYTES];
    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            return redactor.finish();
        }
        redactor.write_chunk(&buffer[..read])?;
    }
}

pub(crate) fn read_ready_line<R: BufRead>(reader: &mut R) -> Result<String, String> {
    let mut bytes = Vec::with_capacity(256);
    let result = reader
        .take((MAX_READY_LINE_BYTES + 1) as u64)
        .read_until(b'\n', &mut bytes);
    match result {
        Ok(0) => Err("sidecar exited before ready".to_string()),
        Ok(_) if bytes.len() > MAX_READY_LINE_BYTES || !bytes.ends_with(b"\n") => {
            Err("invalid sidecar ready message".to_string())
        }
        Ok(_) => String::from_utf8(bytes).map_err(|_| "invalid sidecar ready message".to_string()),
        Err(_) => Err("failed to read sidecar ready message".to_string()),
    }
}
