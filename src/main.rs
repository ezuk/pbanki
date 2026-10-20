use common::*;

use std::cell::RefCell;
use std::ffi::{CStr, CString, c_char};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use inkview::Event;
use inkview::bindings::{Inkview, KBD_NORMAL, KBD_PASSWORD};

const KEYBOARD_MAX_LEN: usize = 8192;

/// Result of the last keyboard: `Some(None)` when it was cancelled.
static KEYBOARD_RESULT: Mutex<Option<Option<String>>> = Mutex::new(None);

unsafe extern "C" fn keyboard_closed(text: *mut c_char) {
    let text = (!text.is_null()).then(|| {
        unsafe { CStr::from_ptr(text) }
            .to_string_lossy()
            .into_owned()
    });
    *KEYBOARD_RESULT.lock().unwrap() = Some(text);
}

/// Turns on Wi-Fi with the default network.
fn connect_network(iv: &'static Inkview) -> Result<(), String> {
    match unsafe { iv.NetConnect(std::ptr::null()) } {
        0 => Ok(()),
        code => Err(format!("Couldn't turn on Wi-Fi (error {code}).")),
    }
}

/// Opens the PocketBook keyboard and polls for its result on the UI thread.
fn keyboard_prompt(iv: &'static Inkview) -> TextPrompt {
    let timer = Rc::new(slint::Timer::default());
    Rc::new(move |request: TextRequest, done| {
        *KEYBOARD_RESULT.lock().unwrap() = None;

        // The keyboard keeps these pointers while open, so they are leaked
        let mut buffer = vec![0u8; KEYBOARD_MAX_LEN];
        let current = request.current.as_bytes();
        let mut len = current.len().min(KEYBOARD_MAX_LEN - 1);
        while !request.current.is_char_boundary(len) {
            len -= 1;
        }
        buffer[..len].copy_from_slice(&current[..len]);
        let buffer: &'static mut [u8] = Box::leak(buffer.into_boxed_slice());
        let title: &'static CString =
            Box::leak(Box::new(CString::new(request.title).unwrap_or_default()));
        let flags = if request.password {
            KBD_PASSWORD
        } else {
            KBD_NORMAL
        };

        unsafe {
            iv.OpenKeyboard(
                title.as_ptr(),
                buffer.as_mut_ptr().cast(),
                (KEYBOARD_MAX_LEN - 1) as i32,
                flags as i32,
                Some(keyboard_closed),
            );
        }

        let done = RefCell::new(Some(done));
        let timer_weak = Rc::downgrade(&timer);
        timer.start(
            slint::TimerMode::Repeated,
            Duration::from_millis(200),
            move || {
                let Some(text) = KEYBOARD_RESULT.lock().unwrap().take() else {
                    return;
                };
                if let Some(done) = done.borrow_mut().take() {
                    done(text);
                }
                if let Some(timer) = timer_weak.upgrade() {
                    timer.stop();
                }
            },
        );
    })
}

fn main() {
    let iv = Box::leak(Box::new(inkview::load())) as &_;

    let (evt_tx, evt_rx) = std::sync::mpsc::channel();
    let reopened = Arc::new(AtomicBool::new(false));
    let reopened_for_ui = reopened.clone();

    std::thread::spawn({
        move || {
            if evt_rx.recv().unwrap() != Event::Init {
                panic!("expected init event first");
            }

            // I hope this thing lives as long as the process
            let screen = inkview::screen::Screen::new(iv);

            let display = inkview_slint::Backend::new(screen, evt_rx);

            slint::platform::set_platform(Box::new(display)).unwrap();

            let config = Config::load_or_create("/mnt/ext1/applications/pbanki/config.toml")
                .unwrap_or_else(|e| {
                    eprintln!("Config error: {e}, using defaults");
                    Config::default()
                });

            let ui = MainWindow::new().unwrap();
            let hooks = PlatformHooks {
                text_prompt: Some(keyboard_prompt(iv)),
                connect_network: Some(Rc::new(move || connect_network(iv))),
                reopened: Some(reopened_for_ui),
                ..PlatformHooks::default()
            };
            setup_ui(&ui, &config, hooks);

            ui.run().unwrap();
        }
    });

    inkview::iv_main(iv, {
        move |evt| {
            if matches!(evt, Event::Foreground { .. }) {
                reopened.store(true, Ordering::Relaxed);
            }
            if evt_tx.send(evt).is_err() {
                unsafe {
                    iv.CloseApp();
                }
            }

            Some(())
        }
    })
}
