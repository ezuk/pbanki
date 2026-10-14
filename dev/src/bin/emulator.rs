//! Renders the app headlessly as it appears on a PocketBook Verse (PB629).
//!
//! Usage: cargo run -p dev --bin emulator -- <app dir> <output dir> <steps...>
//!
//! `<app dir>` holds `collection/collection.anki2` (and optionally `inbox.jsonl`
//! and `config.toml`);
//! it is copied to a temporary directory so runs are repeatable.
//!
//! Steps:
//!   shot:<name>     save <output dir>/<name>.png
//!   tap:<x>,<y>     tap at physical pixel coordinates
//!   swipe:<x1>,<y1>,<x2>,<y2>  swipe from one point to another
//!   key:<name>      press left|right|up|down|pageup|pagedown|menu|return
//!   type:<text>     text the next keyboard prompt returns
//!   wait:<ms>       let timers and background work run
//!   reopen:<file>   queue <file> as the KOReader inbox, then bring the app
//!                   back to the foreground

use std::cell::RefCell;
use std::collections::VecDeque;
use std::fs;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::time::Duration;

use common::{Config, MainWindow, PlatformHooks, TextPrompt, setup_ui};
use slint::platform::software_renderer::{MinimalSoftwareWindow, RepaintBufferType};
use slint::platform::{Key, Platform, PointerEventButton, WindowAdapter, WindowEvent};
use slint::{
    ComponentHandle, LogicalPosition, PhysicalSize, PlatformError, Rgb8Pixel, SharedString,
};

const WIDTH: u32 = 758;
const HEIGHT: u32 = 1024;
const SCALE_FACTOR: f32 = common::api::DEVICE_SCALE_FACTOR;
const GRAY_LEVELS: u8 = 16;

struct Emulator {
    window: Rc<MinimalSoftwareWindow>,
}

impl Platform for Emulator {
    fn create_window_adapter(&self) -> Result<Rc<dyn WindowAdapter>, PlatformError> {
        Ok(self.window.clone())
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [app_dir, out_dir, steps @ ..] = args.as_slice() else {
        return Err("usage: emulator <app dir> <output dir> <steps...>".into());
    };
    let out_dir = PathBuf::from(out_dir);
    fs::create_dir_all(&out_dir)?;

    let work_dir = std::env::temp_dir().join(format!("pbanki-emulator-{}", std::process::id()));
    copy_dir(Path::new(app_dir), &work_dir)?;

    let window = MinimalSoftwareWindow::new(RepaintBufferType::NewBuffer);
    slint::platform::set_platform(Box::new(Emulator {
        window: window.clone(),
    }))
    .map_err(|e| format!("{e:?}"))?;
    window.dispatch_event(WindowEvent::ScaleFactorChanged {
        scale_factor: SCALE_FACTOR,
    });
    window.set_size(PhysicalSize::new(WIDTH, HEIGHT));

    let config_path = work_dir.join("config.toml");
    let mut config = if config_path.exists() {
        Config::load_or_create(&config_path).map_err(|e| e.to_string())?
    } else {
        Config::default()
    };
    config.general.collection_path = work_dir.join("collection").to_string_lossy().into();
    config.path = Some(config_path);

    // Stands in for the PocketBook keyboard
    let typed: Rc<RefCell<VecDeque<String>>> = Rc::default();
    let typed_for_prompt = typed.clone();
    let prompt: TextPrompt = Rc::new(move |_request, done| {
        done(typed_for_prompt.borrow_mut().pop_front());
    });

    let reopened = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let ui = MainWindow::new()?;
    // Stands in for AnkiWeb: the password "wrong" fails, anything else passes
    let hooks = PlatformHooks {
        text_prompt: Some(prompt),
        connect_network: None,
        login: std::sync::Arc::new(|_username: &str, password: &str| {
            if password == "wrong" {
                Err("AnkiWeb rejected the username or password.".into())
            } else {
                Ok("emulator-token".into())
            }
        }),
        sync: std::sync::Arc::new(fake_sync),
        reopened: Some(reopened.clone()),
        open_in_koreader: None,
    };
    setup_ui(&ui, &config, hooks);
    ui.show()?;

    for step in steps {
        let (action, arg) = step.split_once(':').unwrap_or((step, ""));
        match action {
            "shot" => {
                let path = out_dir.join(format!("{arg}.png"));
                save_png(&window, &path)?;
                println!("{}", path.display());
            }
            "tap" => {
                let [x, y] = parse_numbers::<2>(arg)?;
                let position = logical(x, y);
                let button = PointerEventButton::Left;
                window.dispatch_event(WindowEvent::PointerPressed { position, button });
                settle();
                window.dispatch_event(WindowEvent::PointerReleased { position, button });
            }
            "swipe" => {
                let [x1, y1, x2, y2] = parse_numbers::<4>(arg)?;
                let button = PointerEventButton::Left;
                window.dispatch_event(WindowEvent::PointerPressed {
                    position: logical(x1, y1),
                    button,
                });
                for i in 1..=10 {
                    let x = x1 + (x2 - x1) * i / 10;
                    let y = y1 + (y2 - y1) * i / 10;
                    window.dispatch_event(WindowEvent::PointerMoved {
                        position: logical(x, y),
                    });
                    settle();
                }
                window.dispatch_event(WindowEvent::PointerReleased {
                    position: logical(x2, y2),
                    button,
                });
            }
            "type" => typed.borrow_mut().push_back(arg.to_owned()),
            "reopen" => {
                fs::copy(arg, work_dir.join("inbox.jsonl"))?;
                reopened.store(true, std::sync::atomic::Ordering::Relaxed);
            }
            "wait" => {
                let ms: u64 = arg.parse().map_err(|_| format!("bad wait: {arg}"))?;
                let until = std::time::Instant::now() + Duration::from_millis(ms);
                while std::time::Instant::now() < until {
                    settle();
                }
            }
            "key" => {
                let text: SharedString = key(arg)?.into();
                window.dispatch_event(WindowEvent::KeyPressed { text: text.clone() });
                window.dispatch_event(WindowEvent::KeyReleased { text });
            }
            _ => return Err(format!("unknown step: {step}").into()),
        }
        settle();
    }

    drop(ui);
    if let Ok(saved) = fs::read_to_string(work_dir.join("config.toml")) {
        println!("Saved config:\n{saved}");
    }
    let _ = fs::remove_dir_all(&work_dir);
    Ok(())
}

fn logical(x: i32, y: i32) -> LogicalPosition {
    LogicalPosition::new(x as f32 / SCALE_FACTOR, y as f32 / SCALE_FACTOR)
}

fn key(name: &str) -> Result<Key, String> {
    Ok(match name {
        "left" => Key::LeftArrow,
        "right" => Key::RightArrow,
        "up" => Key::UpArrow,
        "down" => Key::DownArrow,
        "pageup" => Key::PageUp,
        "pagedown" => Key::PageDown,
        "menu" => Key::Menu,
        "return" => Key::Return,
        _ => return Err(format!("unknown key: {name}")),
    })
}

fn parse_numbers<const N: usize>(arg: &str) -> Result<[i32; N], String> {
    let numbers: Vec<i32> = arg
        .split(',')
        .map(|n| n.trim().parse().map_err(|_| format!("bad number in {arg}")))
        .collect::<Result<_, _>>()?;
    numbers
        .try_into()
        .map_err(|_| format!("expected {N} numbers in {arg}"))
}

/// Runs pending timers and animations.
fn settle() {
    for _ in 0..5 {
        slint::platform::update_timers_and_animations();
        std::thread::sleep(Duration::from_millis(60));
    }
    slint::platform::update_timers_and_animations();
}

/// Renders the window in grayscale, quantized like the e-ink panel.
fn save_png(window: &MinimalSoftwareWindow, path: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let mut buffer = vec![Rgb8Pixel::default(); (WIDTH * HEIGHT) as usize];
    window.request_redraw();
    window.draw_if_needed(|renderer| {
        renderer.render(&mut buffer, WIDTH as usize);
    });

    let step = 255 / u16::from(GRAY_LEVELS - 1);
    let gray: Vec<u8> = buffer
        .iter()
        .map(|p| {
            let value = (u16::from(p.r) + u16::from(p.g) + u16::from(p.b)) / 3;
            (((value + step / 2) / step) * step).min(255) as u8
        })
        .collect();

    let file = fs::File::create(path)?;
    let mut encoder = png::Encoder::new(file, WIDTH, HEIGHT);
    encoder.set_color(png::ColorType::Grayscale);
    encoder.set_depth(png::BitDepth::Eight);
    encoder.write_header()?.write_image_data(&gray)?;
    Ok(())
}

fn copy_dir(from: &Path, to: &Path) -> std::io::Result<()> {
    fs::create_dir_all(to)?;
    for entry in fs::read_dir(from)? {
        let entry = entry?;
        let target = to.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir(&entry.path(), &target)?;
        } else {
            fs::copy(entry.path(), target)?;
        }
    }
    Ok(())
}

/// Stands in for AnkiWeb sync. `PBANKI_EMULATOR_SYNC` picks the result of a
/// normal sync: `ok` (default), `full` (collections can't be merged) or `fail`.
fn fake_sync(
    _col: &common::collection::SharedCollection,
    _login: &common::SyncLogin,
    mode: common::SyncMode,
    progress: &(dyn Fn(common::SyncStep) + Sync),
) -> common::SyncReport {
    use common::{SyncMode, SyncOutcome, SyncReport, SyncStep};
    let pause = || std::thread::sleep(Duration::from_millis(1200));
    let outcome = match mode {
        SyncMode::Normal => {
            progress(SyncStep::Syncing);
            pause();
            match std::env::var("PBANKI_EMULATOR_SYNC").as_deref() {
                Ok("full") => SyncOutcome::FullSyncNeeded {
                    upload_ok: true,
                    download_ok: true,
                },
                Ok("fail") => SyncOutcome::Failed(
                    "Couldn't reach AnkiWeb. Check the Wi-Fi connection.".into(),
                ),
                _ => SyncOutcome::Synced,
            }
        }
        SyncMode::Download => {
            progress(SyncStep::BackingUp);
            pause();
            progress(SyncStep::Downloading);
            pause();
            SyncOutcome::Downloaded
        }
        SyncMode::Upload => {
            progress(SyncStep::Uploading);
            pause();
            SyncOutcome::Uploaded
        }
    };
    SyncReport::new(outcome)
}
