//! On-screen keyboard laid out like KOReader's English keyboard.
//!
//! Tap a key to type it, swipe across it to type the character in that
//! direction, or hold it to pick from a popup. Shift types one capital;
//! holding it locks capitals. ⌥ switches to symbols.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use slint::platform::{Key, WindowEvent};
use slint::{ComponentHandle, Model, ModelRc, SharedString, VecModel};

use crate::{KeyCap, MainWindow};

/// Keys per row at width 1
pub const ROW_UNITS: f32 = 10.0;
/// Shortest finger movement, in logical pixels, that counts as a swipe
const MIN_SWIPE: f32 = 8.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Char,
    Shift,
    Sym,
    Backspace,
    Left,
    Right,
    Up,
    Down,
}

/// Swipe and popup directions, clockwise from north
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    N,
    NE,
    E,
    SE,
    S,
    SW,
    W,
    NW,
}

impl Direction {
    const ALL: [Direction; 8] = [
        Direction::N,
        Direction::NE,
        Direction::E,
        Direction::SE,
        Direction::S,
        Direction::SW,
        Direction::W,
        Direction::NW,
    ];

    /// Direction of a finger movement; `dy` grows downwards.
    pub fn from_movement(dx: f32, dy: f32) -> Option<Direction> {
        if dx.hypot(dy) < MIN_SWIPE {
            return None;
        }
        let degrees = (-dy).atan2(dx).to_degrees();
        let sector = ((90.0 - degrees) / 45.0).round().rem_euclid(8.0) as usize;
        Some(Self::ALL[sector])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct KeyDef {
    pub action: Action,
    pub label: String,
    /// Typed by a tap
    pub text: String,
    /// Typed by a swipe in each direction; empty when there is none
    pub around: [String; 8],
    /// Shows the north character in the corner
    pub show_north: bool,
    pub width: f32,
}

impl KeyDef {
    fn special(action: Action, label: &str, width: f32) -> Self {
        Self {
            action,
            label: label.into(),
            text: String::new(),
            around: Default::default(),
            show_north: false,
            width,
        }
    }

    /// A character key; `around` lists the characters north, northeast and on
    /// clockwise, with `·` for none.
    fn char(text: &str, around: &str) -> Self {
        let mut cells: [String; 8] = Default::default();
        for (cell, c) in cells.iter_mut().zip(around.split(' ')) {
            if c != "·" {
                *cell = c.into();
            }
        }
        Self {
            action: Action::Char,
            label: text.into(),
            text: text.into(),
            show_north: !cells[0].is_empty(),
            around: cells,
            width: 1.0,
        }
    }

    fn letter(lower: char, upper: bool) -> Self {
        let case = |s: &str| {
            if upper {
                s.to_uppercase()
            } else {
                s.to_owned()
            }
        };
        let text = case(&lower.to_string());
        let other = if upper {
            lower.to_string()
        } else {
            lower.to_uppercase().to_string()
        };
        let mut key = Self::char(&text, accents(lower));
        for cell in key.around.iter_mut().skip(1) {
            // ß has no single capital
            if *cell != "ß" {
                *cell = case(cell);
            }
        }
        key.around[0] = other;
        key.show_north = false;
        key
    }

    pub fn typed(&self, direction: Option<Direction>) -> &str {
        match direction {
            Some(d) if !self.around[d as usize].is_empty() => &self.around[d as usize],
            _ => &self.text,
        }
    }

    pub fn has_popup(&self) -> bool {
        self.action == Action::Char && self.around.iter().any(|c| !c.is_empty())
    }

    /// Popup cells in reading order, with the key itself in the middle
    pub fn popup(&self) -> [String; 9] {
        use Direction::*;
        let at = |d: Direction| self.around[d as usize].clone();
        [
            at(NW),
            at(N),
            at(NE),
            at(W),
            self.text.clone(),
            at(E),
            at(SW),
            at(S),
            at(SE),
        ]
    }
}

/// Accented letters around each key: north (filled in later), northeast, clockwise
fn accents(letter: char) -> &'static str {
    match letter {
        'a' => "· á â ä å æ ã à",
        'c' => "· ç ć č · · · ·",
        'e' => "· é ê ë ę ē · è",
        'i' => "· í î ï · · · ì",
        'n' => "· ñ · · · · · ·",
        'o' => "· ó ô ö ø œ õ ò",
        's' => "· ß š · · · · ·",
        'u' => "· ú û ü · · · ù",
        'y' => "· ý · ÿ · · · ·",
        'z' => "· ž ź ż · · · ·",
        _ => "· · · · · · · ·",
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Mode {
    pub shift: bool,
    /// Shift stays on after typing
    pub caps: bool,
    pub sym: bool,
}

/// Keys row by row for a mode
pub fn layout(mode: Mode) -> Vec<Vec<KeyDef>> {
    let chars = |keys: &[(&str, &str)]| -> Vec<KeyDef> {
        keys.iter().map(|(t, a)| KeyDef::char(t, a)).collect()
    };
    let letters =
        |row: &str| -> Vec<KeyDef> { row.chars().map(|c| KeyDef::letter(c, mode.shift)).collect() };
    let shift_label = if mode.caps { "⇪" } else { "⇧" };
    let shift = KeyDef::special(Action::Shift, shift_label, 1.5);
    let backspace = KeyDef::special(Action::Backspace, "⌫", 1.5);

    let mut rows = match (mode.sym, mode.shift) {
        (false, false) => vec![
            chars(&[
                ("1", "! ¡ ? ¿ ' · · ·"),
                ("2", "@ · · · \" · · ·"),
                ("3", "# · · · · · · ·"),
                ("4", "$ € £ · · · · ·"),
                ("5", "% ‰ · · · · · ·"),
                ("6", "^ · · · · · · ·"),
                ("7", "& · · · · · · ·"),
                ("8", "* · · · · · · ·"),
                ("9", "( [ · · · · · {"),
                ("0", ") } · · · · · ]"),
            ]),
            letters("qwertyuiop"),
            [letters("asdfghjkl"), chars(&[(",", "; · · · · · · ·")])].concat(),
            [vec![shift], letters("zxcvbnm"), vec![backspace]].concat(),
        ],
        (false, true) => vec![
            chars(&[
                ("!", "1 ¡ ? ¿ ' · · ·"),
                ("@", "2 · · · \" · · ·"),
                ("#", "3 · · · · · · ·"),
                ("$", "4 € £ · · · · ·"),
                ("%", "5 ‰ · · · · · ·"),
                ("^", "6 · · · · · · ·"),
                ("&", "7 · · · · · · ·"),
                ("*", "8 · · · · · · ·"),
                ("(", "9 [ · · · · · {"),
                (")", "0 } · · · · · ]"),
            ]),
            letters("qwertyuiop"),
            [letters("asdfghjkl"), chars(&[(";", ", · · · · · · ·")])].concat(),
            [vec![shift], letters("zxcvbnm"), vec![backspace]].concat(),
        ],
        (true, false) => vec![
            chars(&[
                ("!", "· · · · · · · ·"),
                ("¡", "· · · · · · · ·"),
                ("-", "— · · · – · · ·"),
                ("_", "· · · · · · · ·"),
                ("\"", "' » ” · ` · “ «"),
                ("=", "≠ · · · ≈ · · ·"),
                ("7", "· · · · · · · ·"),
                ("8", "· · · · · · · ·"),
                ("9", "· · · · · · · ·"),
                ("-", "∓ · · · · · · ·"),
            ]),
            chars(&[
                ("?", "· · · · · · · ·"),
                ("¿", "· · · · · · · ·"),
                ("~", "· · · · · · · ·"),
                ("\\", "· · · · · · · ·"),
                ("|", "¦ · · · · · · ·"),
                ("/", "÷ · · · · · · ·"),
                ("4", "· · · · · · · ·"),
                ("5", "· · · · · · · ·"),
                ("6", "· · · · · · · ·"),
                ("+", "± · · · · · · ·"),
            ]),
            chars(&[
                ("<", "≤ · · · · · · ·"),
                (">", "≥ · · · · · · ·"),
                ("(", "· · · · · · · ·"),
                (")", "· · · · · · · ·"),
                ("#", "№ · · · · · · ·"),
                ("*", "× · · · · · · ·"),
                ("1", "· · · · · · · ·"),
                ("2", "· · · · · · · ·"),
                ("3", "· · · · · · · ·"),
                (",", "; · · · · · · ·"),
            ]),
            [
                vec![shift],
                chars(&[
                    ("{", "· · · · · · · ·"),
                    ("}", "· · · · · · · ·"),
                    ("[", "· · · · · · · ·"),
                    ("]", "· · · · · · · ·"),
                    ("0", "· · · · · · · ·"),
                ]),
                vec![
                    KeyDef::special(Action::Up, "↑", 1.0),
                    KeyDef::special(Action::Down, "↓", 1.0),
                    backspace,
                ],
            ]
            .concat(),
        ],
        (true, true) => vec![
            chars(&[
                ("`", "· · · · · · · ·"),
                ("‘", "· · · · · · · ·"),
                ("’", "· · · · · · · ·"),
                ("“", "· · · · · · · ·"),
                ("”", "· · · · · · · ·"),
                ("≠", "= · · · · · · ·"),
                ("⁷", "· · · · · · · ·"),
                ("⁸", "· · · · · · · ·"),
                ("⁹", "· · · · · · · ·"),
                ("∓", "- · · · · · · ·"),
            ]),
            chars(&[
                ("∂", "√ · · · · · · ·"),
                ("∫", "· · · · · · · ·"),
                ("∇", "· · · · · · · ·"),
                ("∞", "· · · · · · · ·"),
                ("…", "· · · · · · · ·"),
                ("÷", "/ · · · · · · ·"),
                ("⁴", "· · · · · · · ·"),
                ("⁵", "· · · · · · · ·"),
                ("⁶", "· · · · · · · ·"),
                ("±", "+ · · · · · · ·"),
            ]),
            chars(&[
                ("≤", "< · · · · · · ·"),
                ("≥", "> · · · · · · ·"),
                ("†", "‡ · · · · · · ·"),
                ("¶", "§ · · · · · · ·"),
                ("№", "# · · · · · · ·"),
                ("×", "* · · · · · · ·"),
                ("¹", "· · · · · · · ·"),
                ("²", "· · · · · · · ·"),
                ("³", "· · · · · · · ·"),
                (";", ", · · · · · · ·"),
            ]),
            [
                vec![shift],
                chars(&[
                    ("‰", "· · · · · · · ·"),
                    ("°", "· · · · · · · ·"),
                    ("«", "· · · · · · · ·"),
                    ("»", "· · · · · · · ·"),
                    ("⁰", "· · · · · · · ·"),
                ]),
                vec![
                    KeyDef::special(Action::Up, "↑", 1.0),
                    KeyDef::special(Action::Down, "↓", 1.0),
                    backspace,
                ],
            ]
            .concat(),
        ],
    };

    let period = if mode.shift {
        KeyDef::char(":", ". ' ! · - / ? \"")
    } else {
        KeyDef::char(".", ": ' ! · - / ? \"")
    };
    let mut space = KeyDef::special(Action::Char, "␣", 4.0);
    space.text = " ".into();
    let mut enter = KeyDef::special(Action::Char, "⏎", 1.5);
    enter.text = "\n".into();
    rows.push(vec![
        KeyDef::special(Action::Sym, "⌥", 1.5),
        period,
        space,
        KeyDef::special(Action::Left, "←", 1.0),
        KeyDef::special(Action::Right, "→", 1.0),
        enter,
    ]);
    rows
}

#[derive(Default)]
struct State {
    mode: Mode,
    keys: Vec<KeyDef>,
    /// Key whose popup is open
    popup: Option<usize>,
}

impl State {
    fn rebuild(&mut self, model: &VecModel<KeyCap>) {
        let rows = layout(self.mode);
        let mut caps = Vec::new();
        self.keys.clear();
        for (row, keys) in rows.into_iter().enumerate() {
            let mut x = 0.0;
            for key in keys {
                let active = match key.action {
                    Action::Shift => self.mode.shift,
                    Action::Sym => self.mode.sym,
                    _ => false,
                };
                caps.push(KeyCap {
                    label: key.label.as_str().into(),
                    corner: if key.show_north {
                        key.around[0].as_str().into()
                    } else {
                        SharedString::new()
                    },
                    row: row as i32,
                    x,
                    width: key.width,
                    active,
                });
                x += key.width;
                self.keys.push(key);
            }
        }
        // Updates in place so only changed keys redraw
        if model.row_count() == caps.len() {
            for (i, cap) in caps.into_iter().enumerate() {
                if model.row_data(i).as_ref() != Some(&cap) {
                    model.set_row_data(i, cap);
                }
            }
        } else {
            model.set_vec(caps);
        }
    }

    /// Typing a character releases a one-off Shift
    fn typed_char(&mut self) -> bool {
        if self.mode.shift && !self.mode.caps && !self.mode.sym {
            self.mode.shift = false;
            return true;
        }
        false
    }
}

fn send(window: &slint::Window, text: SharedString) {
    window.dispatch_event(WindowEvent::KeyPressed { text: text.clone() });
    window.dispatch_event(WindowEvent::KeyReleased { text });
}

fn send_key(window: &slint::Window, key: Key) {
    send(window, key.into());
}

fn chord(window: &slint::Window, modifier: Key, key: Key) {
    let modifier: SharedString = modifier.into();
    window.dispatch_event(WindowEvent::KeyPressed {
        text: modifier.clone(),
    });
    send_key(window, key);
    window.dispatch_event(WindowEvent::KeyReleased { text: modifier });
}

/// Slint's word-deletion modifier differs on Apple systems
fn word_modifier() -> Key {
    if cfg!(target_vendor = "apple") {
        Key::Alt
    } else {
        Key::Control
    }
}

/// Edits the focused text field once the touch that caused it has finished.
fn later(ui: &slint::Weak<MainWindow>, edit: impl FnOnce(&slint::Window) + 'static) {
    let ui = ui.clone();
    slint::Timer::single_shot(Duration::ZERO, move || {
        if let Some(ui) = ui.upgrade() {
            edit(ui.window());
        }
    });
}

/// Connects the keyboard's keys and popup.
pub fn setup_keyboard(ui: &MainWindow) {
    let state: Rc<RefCell<State>> = Rc::default();
    let model = Rc::new(VecModel::<KeyCap>::default());
    state.borrow_mut().rebuild(&model);
    ui.set_keyboard_keys(ModelRc::from(model.clone()));

    let ui_weak = ui.as_weak();
    let state_for_reset = state.clone();
    let model_for_reset = model.clone();
    ui.on_keyboard_reset(move || {
        let mut state = state_for_reset.borrow_mut();
        state.mode = Mode::default();
        state.popup = None;
        state.rebuild(&model_for_reset);
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_keyboard_popup(ModelRc::default());
        }
    });

    let ui_weak = ui.as_weak();
    let state_for_release = state.clone();
    let model_for_release = model.clone();
    ui.on_key_released(move |index, dx, dy| {
        let mut state = state_for_release.borrow_mut();
        let Some(key) = state.keys.get(index as usize).cloned() else {
            return;
        };
        let direction = Direction::from_movement(dx, dy);
        match key.action {
            Action::Char => {
                let text: SharedString = key.typed(direction).into();
                later(&ui_weak, move |window| send(window, text));
                if state.typed_char() {
                    state.rebuild(&model_for_release);
                }
            }
            Action::Shift => {
                if state.mode.caps {
                    state.mode.caps = false;
                    state.mode.shift = false;
                } else {
                    state.mode.shift = !state.mode.shift;
                }
                state.rebuild(&model_for_release);
            }
            Action::Sym => {
                state.mode.sym = !state.mode.sym;
                state.rebuild(&model_for_release);
            }
            Action::Backspace => later(&ui_weak, move |window| match direction {
                Some(Direction::W) => chord(window, word_modifier(), Key::Backspace),
                Some(Direction::N) => chord(window, word_modifier(), Key::Delete),
                _ => send_key(window, Key::Backspace),
            }),
            Action::Left => later(&ui_weak, |window| send_key(window, Key::LeftArrow)),
            Action::Right => later(&ui_weak, |window| send_key(window, Key::RightArrow)),
            Action::Up => later(&ui_weak, |window| send_key(window, Key::UpArrow)),
            Action::Down => later(&ui_weak, |window| send_key(window, Key::DownArrow)),
        }
    });

    let ui_weak = ui.as_weak();
    let state_for_hold = state.clone();
    let model_for_hold = model.clone();
    ui.on_key_held(move |index| {
        let mut state = state_for_hold.borrow_mut();
        let Some(key) = state.keys.get(index as usize).cloned() else {
            return;
        };
        match key.action {
            Action::Char if key.has_popup() => {
                state.popup = Some(index as usize);
                if let Some(ui) = ui_weak.upgrade() {
                    let cells: Vec<SharedString> =
                        key.popup().iter().map(|c| c.as_str().into()).collect();
                    ui.set_keyboard_popup_key(index);
                    ui.set_keyboard_popup(ModelRc::new(VecModel::from(cells)));
                }
            }
            Action::Char => {
                let text: SharedString = key.text.as_str().into();
                later(&ui_weak, move |window| send(window, text));
                if state.typed_char() {
                    state.rebuild(&model_for_hold);
                }
            }
            Action::Shift => {
                let locked = !state.mode.caps;
                state.mode.caps = locked;
                state.mode.shift = locked;
                state.rebuild(&model_for_hold);
            }
            Action::Sym => {
                state.mode.sym = !state.mode.sym;
                state.rebuild(&model_for_hold);
            }
            Action::Backspace => later(&ui_weak, |window| {
                chord(window, Key::Shift, Key::Home);
                send_key(window, Key::Backspace);
            }),
            Action::Left => later(&ui_weak, |window| send_key(window, Key::Home)),
            Action::Right => later(&ui_weak, |window| send_key(window, Key::End)),
            Action::Up => later(&ui_weak, |window| send_key(window, Key::UpArrow)),
            Action::Down => later(&ui_weak, |window| send_key(window, Key::DownArrow)),
        }
    });

    let ui_weak = ui.as_weak();
    ui.on_keyboard_popup_picked(move |cell| {
        let mut state = state.borrow_mut();
        let Some(key) = state.popup.take().and_then(|i| state.keys.get(i).cloned()) else {
            return;
        };
        let Some(ui) = ui_weak.upgrade() else {
            return;
        };
        ui.set_keyboard_popup(ModelRc::default());
        let Some(text) = key
            .popup()
            .get(cell as usize)
            .filter(|t| !t.is_empty())
            .cloned()
        else {
            return;
        };
        let text: SharedString = text.into();
        later(&ui_weak, move |window| send(window, text));
        if state.typed_char() {
            state.rebuild(&model);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn find(mode: Mode, label: &str) -> KeyDef {
        layout(mode)
            .into_iter()
            .flatten()
            .find(|k| k.label == label)
            .unwrap_or_else(|| panic!("no key {label}"))
    }

    #[test]
    fn every_row_fills_the_width() {
        for sym in [false, true] {
            for shift in [false, true] {
                let mode = Mode {
                    shift,
                    caps: false,
                    sym,
                };
                for row in layout(mode) {
                    let width: f32 = row.iter().map(|k| k.width).sum();
                    assert_eq!(width, ROW_UNITS, "{mode:?}");
                }
            }
        }
    }

    #[test]
    fn letters_follow_shift() {
        let lower = find(Mode::default(), "q");
        assert_eq!(lower.typed(None), "q");
        assert_eq!(lower.typed(Some(Direction::N)), "Q");
        let upper = find(
            Mode {
                shift: true,
                ..Mode::default()
            },
            "E",
        );
        assert_eq!(upper.typed(Some(Direction::NE)), "É");
        assert_eq!(upper.typed(Some(Direction::N)), "e");
    }

    #[test]
    fn swipes_without_a_character_type_the_key() {
        let q = find(Mode::default(), "q");
        assert_eq!(q.typed(Some(Direction::E)), "q");
        assert!(!q.has_popup() || q.around[0] == "Q");
    }

    #[test]
    fn digits_show_their_symbol() {
        let one = find(Mode::default(), "1");
        assert!(one.show_north);
        assert_eq!(one.typed(Some(Direction::N)), "!");
        assert!(!find(Mode::default(), "a").show_north);
    }

    #[test]
    fn popup_puts_the_key_in_the_middle() {
        let a = find(Mode::default(), "a");
        let cells = a.popup();
        assert_eq!(cells[4], "a");
        assert_eq!(cells[1], "A");
        assert_eq!(cells[2], "á");
        assert_eq!(cells[0], "à");
    }

    #[test]
    fn movement_maps_to_directions() {
        assert_eq!(Direction::from_movement(2.0, 2.0), None);
        assert_eq!(Direction::from_movement(0.0, -20.0), Some(Direction::N));
        assert_eq!(Direction::from_movement(20.0, -20.0), Some(Direction::NE));
        assert_eq!(Direction::from_movement(20.0, 0.0), Some(Direction::E));
        assert_eq!(Direction::from_movement(0.0, 20.0), Some(Direction::S));
        assert_eq!(Direction::from_movement(-20.0, 1.0), Some(Direction::W));
        assert_eq!(Direction::from_movement(-20.0, -20.0), Some(Direction::NW));
    }

    #[test]
    fn one_off_shift_releases_after_a_character() {
        let mut state = State {
            mode: Mode {
                shift: true,
                ..Mode::default()
            },
            ..State::default()
        };
        assert!(state.typed_char());
        assert!(!state.mode.shift);

        state.mode = Mode {
            shift: true,
            caps: true,
            sym: false,
        };
        assert!(!state.typed_char());
        assert!(state.mode.shift);
    }
}
