use common::*;

fn main() {
    let config = Config::load_or_create("./pbanki/config.toml").unwrap_or_else(|e| {
        eprintln!("Config error: {e}, using defaults");
        let mut cfg = Config::default();
        cfg.general.collection_path = "./pbanki/collection".into();
        cfg
    });

    let ui = MainWindow::new().unwrap();
    setup_ui(&ui, &config, PlatformHooks::default());

    ui.run().unwrap();
}
