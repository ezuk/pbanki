use serde::{Deserialize, Serialize};
use std::fs;
use std::ops::RangeInclusive;
use std::path::{Path, PathBuf};

/// Allowed card text sizes in device pixels.
pub const FONT_SIZES: RangeInclusive<u32> = 12..=40;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub general: GeneralConfig,
    #[serde(default)]
    pub display: DisplayConfig,
    pub ankiweb: AnkiWebConfig,
    /// File this config was loaded from and is saved to.
    #[serde(skip)]
    pub path: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GeneralConfig {
    pub collection_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct DisplayConfig {
    /// Card text size in screen pixels.
    pub font_size: u32,
}

impl Default for DisplayConfig {
    fn default() -> Self {
        Self { font_size: 20 }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnkiWebConfig {
    #[serde(default)]
    pub username: String,
    #[serde(default)]
    pub password: String,
    #[serde(default)]
    pub token: Option<String>,
    /// Sync server AnkiWeb assigned to the account
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub endpoint: Option<String>,
    /// Sync when the app starts
    #[serde(default)]
    pub auto_sync: bool,
    #[serde(default)]
    pub sync_on_exit: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            general: GeneralConfig {
                collection_path: "/mnt/ext1/applications/pbanki/collection".into(),
            },
            display: DisplayConfig::default(),
            ankiweb: AnkiWebConfig {
                username: String::new(),
                password: String::new(),
                token: None,
                endpoint: None,
                auto_sync: false,
                sync_on_exit: false,
            },
            path: None,
        }
    }
}

impl DisplayConfig {
    /// The font size, limited to [`FONT_SIZES`].
    pub fn font_size(&self) -> u32 {
        self.font_size.clamp(*FONT_SIZES.start(), *FONT_SIZES.end())
    }
}

impl AnkiWebConfig {
    /// The saved session token, if one is set.
    pub fn token(&self) -> Option<&str> {
        self.token.as_deref().filter(|t| !t.is_empty())
    }

    /// The saved login, or `None` when sync is off.
    pub fn sync_login(&self) -> Option<crate::sync::SyncLogin> {
        if self.username.is_empty() || (self.password.is_empty() && self.token().is_none()) {
            return None;
        }
        Some(crate::sync::SyncLogin {
            username: self.username.clone(),
            password: self.password.clone(),
            token: self.token().map(str::to_owned),
            endpoint: self.endpoint.clone(),
        })
    }
}

const DEFAULT_CONFIG_WITH_COMMENTS: &str = r#"# pbAnki Configuration File
# Generated automatically - edit with care

[general]
# Collection path (relative to app directory or absolute)
collection_path = "/mnt/ext1/applications/pbanki/collection"

[display]
# Card text size in screen pixels
font_size = 20

[ankiweb]
# AnkiWeb synchronization settings
# Leave empty to disable sync

# AnkiWeb username (email)
username = ""

# Password (plain text - will be encrypted in future versions)
# WARNING: Do not share this file if password is filled
password = ""

# Session token (populated after successful login)
# This avoids storing password long-term
token = ""

# Sync automatically on app start
auto_sync = false

# Sync automatically after session ends
sync_on_exit = false
"#;

impl Config {
    pub fn load_or_create(path: impl AsRef<Path>) -> Result<Self, Box<dyn std::error::Error>> {
        let path = path.as_ref();

        let mut config = if path.exists() {
            toml::from_str(&fs::read_to_string(path)?)?
        } else {
            write_file(path, DEFAULT_CONFIG_WITH_COMMENTS)?;
            Config::default()
        };

        config.path = Some(path.to_path_buf());
        Ok(config)
    }

    /// Replaces the AnkiWeb login and its session key.
    pub fn set_login(&mut self, username: &str, password: &str, token: Option<String>) {
        self.ankiweb.username = username.trim().to_owned();
        self.ankiweb.password = password.to_owned();
        self.ankiweb.token = token;
        self.ankiweb.endpoint = None;
    }

    /// Sets the card text size, limited to [`FONT_SIZES`].
    pub fn set_font_size(&mut self, font_size: u32) {
        self.display.font_size = font_size;
        self.display.font_size = self.display.font_size();
    }

    pub fn save(&self) -> Result<(), Box<dyn std::error::Error>> {
        let path = self
            .path
            .as_deref()
            .ok_or("config has no file path to save to")?;
        write_file(path, &toml::to_string_pretty(self)?)
    }
}

fn write_file(path: &Path, contents: &str) -> Result<(), Box<dyn std::error::Error>> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, contents)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn creates_commented_default_file_when_missing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested/config.toml");

        let config = Config::load_or_create(&path).unwrap();

        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            DEFAULT_CONFIG_WITH_COMMENTS
        );
        assert_eq!(config.path.as_deref(), Some(path.as_path()));
    }

    #[test]
    fn default_file_parses_to_default_values() {
        let parsed: Config = toml::from_str(DEFAULT_CONFIG_WITH_COMMENTS).unwrap();
        let default = Config::default();

        assert_eq!(
            parsed.general.collection_path,
            default.general.collection_path
        );
        assert!(!parsed.ankiweb.auto_sync);
        assert!(!parsed.ankiweb.sync_on_exit);
        assert_eq!(parsed.display.font_size, default.display.font_size);
        assert_eq!(parsed.ankiweb.token(), None);
    }

    #[test]
    fn empty_token_counts_as_missing() {
        let mut ankiweb = Config::default().ankiweb;
        assert_eq!(ankiweb.token(), None);
        ankiweb.token = Some(String::new());
        assert_eq!(ankiweb.token(), None);
        ankiweb.token = Some("abc".into());
        assert_eq!(ankiweb.token(), Some("abc"));
    }

    #[test]
    fn missing_ankiweb_fields_use_defaults() {
        let config: Config =
            toml::from_str("[general]\ncollection_path = \"/tmp/col\"\n[ankiweb]\n").unwrap();
        assert_eq!(config.general.collection_path, "/tmp/col");
        assert_eq!(config.ankiweb.username, "");
        assert_eq!(config.display.font_size, 20);
        assert_eq!(config.ankiweb.token(), None);
    }

    #[test]
    fn save_round_trips_to_loaded_path() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.toml");

        let mut config = Config::load_or_create(&path).unwrap();
        config.general.collection_path = "/books/col".into();
        config.ankiweb.token = Some("token123".into());
        config.save().unwrap();

        let reloaded = Config::load_or_create(&path).unwrap();
        assert_eq!(reloaded.general.collection_path, "/books/col");
        assert_eq!(reloaded.ankiweb.token(), Some("token123"));
    }

    #[test]
    fn font_size_is_clamped() {
        let mut display = DisplayConfig { font_size: 2 };
        assert_eq!(display.font_size(), 12);
        display.font_size = 99;
        assert_eq!(display.font_size(), 40);
        display.font_size = 22;
        assert_eq!(display.font_size(), 22);
    }

    #[test]
    fn set_login_replaces_token() {
        let mut config = Config::default();
        config.ankiweb.token = Some("old".into());

        config.set_login(" me@example.com ", "secret", Some("new".into()));
        assert_eq!(config.ankiweb.username, "me@example.com");
        assert_eq!(config.ankiweb.password, "secret");
        assert_eq!(config.ankiweb.token(), Some("new"));

        config.set_login("", "", None);
        assert_eq!(config.ankiweb.token(), None);

        config.set_font_size(100);
        assert_eq!(config.display.font_size, 40);
    }

    #[test]
    fn save_without_path_fails() {
        assert!(Config::default().save().is_err());
    }

    #[test]
    fn malformed_file_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.toml");
        fs::write(&path, "[general\nlanguage = ").unwrap();

        assert!(Config::load_or_create(&path).is_err());
    }
}
