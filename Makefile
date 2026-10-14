all:
	cargo zigbuild --target armv7-unknown-linux-gnueabi.2.23 --profile dev -p pbanki --features=sdk-6-8

release:
	cargo zigbuild --target armv7-unknown-linux-gnueabi.2.23 --profile release -p pbanki --features=sdk-6-8

# Lints the PocketBook crate, which the default workspace checks skip
clippy-device:
	cargo-zigbuild clippy --target armv7-unknown-linux-gnueabi.2.23 -p pbanki --features=sdk-6-8 -- -D warnings
