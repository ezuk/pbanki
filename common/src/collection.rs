//! The open Anki collection, shared between the UI thread and the sync thread.

use std::ops::{Deref, DerefMut};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

use anki::collection::{Collection, CollectionBuilder};
use anki::error::Result as AnkiResult;
use anki::prelude::I18n;

/// Opens the collection file with English strings.
pub fn open_collection(path: &Path) -> AnkiResult<Collection> {
    CollectionBuilder::new(path)
        .set_tr(I18n::new(&["en"]))
        .build()
}

#[derive(Clone)]
pub struct SharedCollection {
    // Empty only while a full sync replaces the file
    slot: Arc<Mutex<Option<Collection>>>,
    path: PathBuf,
}

impl SharedCollection {
    pub fn new(col: Collection, path: PathBuf) -> Self {
        Self {
            slot: Arc::new(Mutex::new(Some(col))),
            path,
        }
    }

    /// Path of `collection.anki2`.
    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn lock(&self) -> CollectionGuard<'_> {
        CollectionGuard(self.slot.lock().unwrap_or_else(|e| e.into_inner()))
    }

    /// Hands the collection to `f`, which consumes it, then opens
    /// the file again. Others wait on the lock meanwhile.
    pub fn replace<R>(&self, f: impl FnOnce(Collection) -> R) -> AnkiResult<R> {
        let mut slot = self.slot.lock().unwrap_or_else(|e| e.into_inner());
        let col = slot.take().expect("collection is open");
        let result = f(col);
        *slot = Some(open_collection(&self.path)?);
        Ok(result)
    }
}

pub struct CollectionGuard<'a>(MutexGuard<'a, Option<Collection>>);

impl Deref for CollectionGuard<'_> {
    type Target = Collection;

    fn deref(&self) -> &Collection {
        self.0.as_ref().expect("collection is open")
    }
}

impl DerefMut for CollectionGuard<'_> {
    fn deref_mut(&mut self) -> &mut Collection {
        self.0.as_mut().expect("collection is open")
    }
}
