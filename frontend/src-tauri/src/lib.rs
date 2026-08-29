use serde::Serialize;
use std::io::Read;
use std::path::PathBuf;

#[derive(Serialize)]
struct DeviceInfo {
  device_id: String,
  hostname: String,
}

#[tauri::command]
fn get_device_info() -> DeviceInfo {
  let hostname = std::env::var("COMPUTERNAME")
    .or_else(|_| std::env::var("HOSTNAME"))
    .unwrap_or_else(|_| "unknown-host".into());

  let machine = read_windows_machine_guid().unwrap_or_else(|| {
    format!(
      "fallback-{}",
      hostname
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect::<String>()
        .to_lowercase()
    )
  });

  DeviceInfo {
    device_id: format!("DEV-{}", machine.trim().to_uppercase()),
    hostname,
  }
}

#[cfg(windows)]
fn read_windows_machine_guid() -> Option<String> {
  // Read the registry value directly via the Win32 API instead of spawning
  // `reg.exe` and text-parsing its output — the subprocess approach was
  // fragile (locale-dependent formatting, and some antivirus/EDR software
  // blocks child processes querying these values as a fingerprinting IOC).
  use winreg::enums::HKEY_LOCAL_MACHINE;
  use winreg::RegKey;

  let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);

  // Prefer SQMClient\MachineId — this is the same "Device ID" shown in
  // Windows Settings > System > About, so it matches what a customer can
  // independently read off their own PC and report to an admin.
  let sqm = hklm
    .open_subkey(r"SOFTWARE\Microsoft\SQMClient")
    .ok()
    .and_then(|key| key.get_value::<String, _>("MachineId").ok());
  if let Some(id) = sqm {
    let id = id.trim().trim_start_matches('{').trim_end_matches('}');
    if !id.is_empty() {
      return Some(id.to_string());
    }
  }

  // Fall back to Cryptography\MachineGuid if SQMClient is unavailable.
  let guid = hklm
    .open_subkey(r"SOFTWARE\Microsoft\Cryptography")
    .ok()?
    .get_value::<String, _>("MachineGuid")
    .ok()?;
  let guid = guid.trim();
  if guid.is_empty() {
    None
  } else {
    Some(guid.to_string())
  }
}

#[cfg(not(windows))]
fn read_windows_machine_guid() -> Option<String> {
  None
}

fn downloads_dir() -> Result<PathBuf, String> {
  if let Some(dir) = dirs::download_dir() {
    return Ok(dir);
  }
  let home = std::env::var_os("USERPROFILE")
    .or_else(|| std::env::var_os("HOME"))
    .ok_or_else(|| "Could not find user home folder".to_string())?;
  let downloads = PathBuf::from(&home).join("Downloads");
  if downloads.is_dir() {
    Ok(downloads)
  } else {
    Ok(PathBuf::from(home))
  }
}

fn unique_path(dir: &std::path::Path, filename: &str) -> PathBuf {
  let candidate = dir.join(filename);
  if !candidate.exists() {
    return candidate;
  }
  let stem = candidate
    .file_stem()
    .map(|s| s.to_string_lossy().into_owned())
    .unwrap_or_else(|| "download".into());
  let ext = candidate
    .extension()
    .map(|e| format!(".{}", e.to_string_lossy()))
    .unwrap_or_default();
  for i in 1..1000 {
    let next = dir.join(format!("{stem} ({i}){ext}"));
    if !next.exists() {
      return next;
    }
  }
  dir.join(format!("{stem}-new{ext}"))
}

fn safe_filename(filename: &str) -> String {
  filename
    .rsplit(['/', '\\'])
    .next()
    .unwrap_or("download.bin")
    .chars()
    .map(|c| if r#"<>:"|?*"#.contains(c) { '_' } else { c })
    .collect::<String>()
}

fn write_download(filename: &str, bytes: &[u8]) -> Result<String, String> {
  if bytes.is_empty() {
    return Err("Download was empty".into());
  }
  let safe_name = safe_filename(filename);
  let dir = downloads_dir()?;
  let path = unique_path(&dir, &safe_name);
  std::fs::write(&path, bytes).map_err(|e| format!("Could not write file: {e}"))?;
  #[cfg(windows)]
  {
    use std::process::Command;
    let arg = path.to_string_lossy().into_owned();
    let _ = Command::new("explorer").args(["/select,", &arg]).spawn();
  }
  Ok(path.to_string_lossy().into_owned())
}

fn read_response_body(resp: ureq::Response) -> Result<Vec<u8>, String> {
  let mut reader = resp.into_reader();
  let mut bytes = Vec::new();
  reader
    .read_to_end(&mut bytes)
    .map_err(|e| format!("Could not read download body: {e}"))?;
  if bytes.is_empty() {
    return Err("Download returned an empty file".into());
  }
  Ok(bytes)
}

/// Save bytes already fetched in the WebView (small files only).
#[tauri::command]
fn save_download(filename: String, bytes: Vec<u8>) -> Result<String, String> {
  write_download(&filename, &bytes)
}

fn download_url_blocking(
  url: String,
  filename: String,
  authorization: Option<String>,
) -> Result<String, String> {
  let mut req = ureq::get(&url);
  if let Some(auth) = authorization.filter(|s| !s.is_empty()) {
    req = req.set("Authorization", &auth);
  }
  // No client timeout — large inspection reports can take several minutes.
  let resp = req
    .call()
    .map_err(|e| format!("Download failed: {e}"))?;
  if !(200..300).contains(&resp.status()) {
    return Err(format!("Download failed with HTTP {}", resp.status()));
  }
  let bytes = read_response_body(resp)?;
  write_download(&filename, &bytes)
}

/// Fetch a URL from Rust and write into Downloads (reliable for large PDFs in WebView2).
/// Runs off the UI thread so the window does not freeze as "(Not Responding)".
#[tauri::command]
async fn download_url(
  url: String,
  filename: String,
  authorization: Option<String>,
) -> Result<String, String> {
  tauri::async_runtime::spawn_blocking(move || download_url_blocking(url, filename, authorization))
    .await
    .map_err(|e| format!("Download task failed: {e}"))?
}

fn download_post_blocking(
  url: String,
  filename: String,
  authorization: Option<String>,
  body: String,
) -> Result<String, String> {
  let mut req = ureq::post(&url).set("Content-Type", "application/json");
  if let Some(auth) = authorization.filter(|s| !s.is_empty()) {
    req = req.set("Authorization", &auth);
  }
  // No client timeout — wait until the API finishes the PDF.
  match req.send_string(&body) {
    Ok(resp) => {
      if !(200..300).contains(&resp.status()) {
        return Err(format!("Report PDF download failed with HTTP {}", resp.status()));
      }
      let bytes = read_response_body(resp)?;
      write_download(&filename, &bytes)
    }
    Err(ureq::Error::Status(code, resp)) => {
      let detail = resp
        .into_string()
        .unwrap_or_default()
        .chars()
        .take(300)
        .collect::<String>();
      Err(format!(
        "Report PDF download failed with HTTP {code}{}",
        if detail.trim().is_empty() {
          String::new()
        } else {
          format!(": {detail}")
        }
      ))
    }
    Err(e) => Err(format!("Report PDF download failed: {e}")),
  }
}

/// POST JSON then save response body (inspection report PDF).
#[tauri::command]
async fn download_post(
  url: String,
  filename: String,
  authorization: Option<String>,
  body: String,
) -> Result<String, String> {
  tauri::async_runtime::spawn_blocking(move || {
    download_post_blocking(url, filename, authorization, body)
  })
  .await
  .map_err(|e| format!("Download task failed: {e}"))?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .invoke_handler(tauri::generate_handler![
      get_device_info,
      save_download,
      download_url,
      download_post
    ])
    .setup(|app| {
      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      }
      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
