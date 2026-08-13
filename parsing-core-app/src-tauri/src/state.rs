use serde::Serialize;

pub const MAX_STATUS_LOGS: usize = 200;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceError {
    pub category: String,
    pub message: String,
}

#[derive(Default)]
pub struct AppState {
    pub port: u16,
    pub session_token: String,
    pub starting: bool,
    pub running: bool,
    pub desired_running: bool,
    pub manual_stopped: bool,
    pub service_state: String,
    pub error: Option<ServiceError>,
    pub log_path: Option<String>,
    pub logs: Vec<String>,
    pub health_failures: u8,
    pub generation: u64,
    pub sidecar_child: Option<std::process::Child>,
    pub sidecar_process_group: Option<i32>,
    pub sidecar_log_threads: Vec<std::thread::JoinHandle<()>>,
    pub reserved_listener: Option<std::net::TcpListener>,
}

impl AppState {
    pub fn push_log(&mut self, message: impl Into<String>) {
        if self.logs.len() >= MAX_STATUS_LOGS {
            let remove = self.logs.len() + 1 - MAX_STATUS_LOGS;
            self.logs.drain(..remove);
        }
        self.logs.push(message.into());
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StatusPayload {
    pub port: u16,
    pub state: String,
    pub error: Option<ServiceError>,
    pub log_path: Option<String>,
    pub logs: Vec<String>,
    pub desired_running: bool,
    pub manual_stopped: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ApiConfig {
    pub api_base: String,
    pub session_token: String,
}

pub fn generate_session_token() -> String {
    [
        uuid::Uuid::new_v4().simple().to_string(),
        uuid::Uuid::new_v4().simple().to_string(),
        uuid::Uuid::new_v4().simple().to_string(),
    ]
    .concat()
}

pub fn ready_api_config(state: &AppState) -> Result<ApiConfig, String> {
    if !state.running || state.port == 0 || state.session_token.len() < 32 {
        return Err("service not ready".into());
    }
    Ok(ApiConfig {
        api_base: format!("http://127.0.0.1:{}", state.port),
        session_token: state.session_token.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::{generate_session_token, ready_api_config, AppState, MAX_STATUS_LOGS};

    #[test]
    fn session_tokens_are_strong_and_unique() {
        let first = generate_session_token();
        let second = generate_session_token();

        assert!(first.len() >= 64);
        assert!(second.len() >= 64);
        assert_ne!(first, second);
    }

    #[test]
    fn api_config_is_unavailable_until_ready() {
        let state = AppState {
            port: 43127,
            session_token: "session-token-0123456789abcdef0123456789abcdef".into(),
            running: false,
            ..Default::default()
        };

        assert!(matches!(
            ready_api_config(&state),
            Err(error) if error == "service not ready"
        ));
    }

    #[test]
    fn ready_api_config_includes_the_in_memory_session() {
        let state = AppState {
            port: 43127,
            session_token: "session-token-0123456789abcdef0123456789abcdef".into(),
            running: true,
            ..Default::default()
        };

        let config = ready_api_config(&state).unwrap();
        assert_eq!(config.api_base, "http://127.0.0.1:43127");
        assert_eq!(config.session_token, state.session_token);
    }

    #[test]
    fn status_logs_keep_only_the_newest_bounded_entries() {
        let mut state = AppState::default();

        for index in 0..(MAX_STATUS_LOGS + 17) {
            state.push_log(format!("entry-{index}"));
        }

        assert_eq!(state.logs.len(), MAX_STATUS_LOGS);
        assert_eq!(state.logs.first().map(String::as_str), Some("entry-17"));
        assert_eq!(
            state.logs.last().map(String::as_str),
            Some(format!("entry-{}", MAX_STATUS_LOGS + 16).as_str())
        );
    }
}
