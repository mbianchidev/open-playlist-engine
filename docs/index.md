<section class="hero" markdown="1">
<div class="hero-copy" markdown="1">

<span class="eyebrow">The first Open Playlist reference implementation</span>

# Your playlists should outlive your music service

Open Playlist Engine is a self-hosted hub for moving, creating, organizing,
backing up, and exporting music-library metadata. Connect a source and a target;
the engine handles provider differences while keeping every match visible.

[See how the engine works](HOW_IT_WORKS.md){ .button .button-primary }
[Explore the Open Playlist standard](https://mbianchidev.github.io/open-playlist/){ .button .button-secondary }

</div>

<div class="route-map" role="img" aria-label="Music providers and local files connect through Open Playlist Engine to any target provider or portable export">
  <div class="route-sources">
    <span>Spotify</span>
    <span>Tidal</span>
    <span>YouTube Music</span>
    <span>Apple Music</span>
    <span>Files · URLs · text</span>
  </div>
  <div class="route-line" aria-hidden="true"></div>
  <div class="route-hub">
    <img src="assets/mark.svg" alt="">
    <strong>Open Playlist</strong>
    <small>universal hub</small>
  </div>
  <div class="route-line route-line-out" aria-hidden="true"></div>
  <div class="route-targets">
    <span>Any supported target</span>
    <span>Portable export</span>
  </div>
</div>
</section>

<section class="value-strip" aria-label="Project principles">
  <p><strong>Metadata only</strong><span>No audio is moved or streamed.</span></p>
  <p><strong>Review before write</strong><span>Uncertain matches wait for a person.</span></p>
  <p><strong>Local first</strong><span>Run the complete stack on infrastructure you control.</span></p>
</section>

## One hub, every route

Provider-to-provider converters grow one integration for every pair. Open Playlist
Engine instead maps each provider to the
[Open Playlist standard](https://mbianchidev.github.io/open-playlist/). A new
provider becomes another spoke and can interoperate with every existing spoke.

<div class="pipeline" markdown="1">
<div markdown="1">

### 01 · Import

Read a connected library, local playlist file, public URL, pasted list, snapshot,
or approved generator draft into a provider-neutral model.

</div>
<div markdown="1">

### 02 · Match

Resolve tracks and library entities using provider identifiers, ISRCs, text
evidence, and prior accepted decisions.

</div>
<div markdown="1">

### 03 · Review

Pause low-confidence results for approval, correction, or skipping. Nothing
uncertain is silently written.

</div>
<div markdown="1">

### 04 · Write

Queue durable, idempotent provider operations and stream item-level progress back
to the browser.

</div>
</div>

[Follow a request through the system →](HOW_IT_WORKS.md){ .text-link }

## Built for real music libraries

<div class="feature-ledger" markdown="1">
<div markdown="1">

### Move without starting over

Migrate playlists, liked tracks, saved albums, and followed or favorite artists
where provider capabilities allow. Partial reruns and duplicate checks protect
work already completed.

[Migration architecture](DESIGN.md#2-phased-flow)

</div>
<div markdown="1">

### Keep a portable copy

Export playlists as CSV, TXT, M3U8, XSPF, or versioned Open Playlist JSON. Create
checksummed local snapshots and restore selected collections through the same
review pipeline.

[Exports](EXPORTING_PLAYLISTS.md) · [Snapshots](SNAPSHOTS.md)

</div>
<div markdown="1">

### Build and maintain

Generate editable playlist drafts with an administrator-controlled model,
synchronize proven routes on a schedule, and run capability-gated cleanup with
preflight checks and durable retries.

[Generator](PLAYLIST_GENERATOR.md) · [Sync](SYNCHRONIZATION.md) ·
[Organizer](PLAYLIST_ORGANIZER.md)

</div>
</div>

## Self-host in one stack

Docker Compose starts PostgreSQL, Valkey, the FastAPI service, background worker,
and React frontend:

```bash
cp .env.example .env
# Replace OPE_SECRET_KEY and add only the provider credentials you need.
docker compose build --no-cache
docker compose up
```

The application is available at `http://localhost:8080`. Start with
[configuration](CONFIGURATION.md), then register the providers you want using the
[connection guide](CONNECTING_PROVIDERS.md).

!!! note "Capability-driven by design"
    The interface enables only operations declared by the selected provider.
    Unsupported library types or destructive actions stay unavailable instead of
    being approximated.

## Read the project documentation

<div class="docs-directory" markdown="1">
<div markdown="1">

### Understand the system

- [How this implementation works](HOW_IT_WORKS.md)
- [Detailed design reference](DESIGN.md)
- [Continuous integration](CI.md)

</div>
<div markdown="1">

### Bring data in and out

- [Local playlist-file imports](LOCAL_FILE_IMPORTS.md)
- [Public URLs and pasted text](IMPORT_SOURCES.md)
- [Portable playlist exports](EXPORTING_PLAYLISTS.md)

</div>
<div markdown="1">

### Operate your library

- [Snapshots](SNAPSHOTS.md)
- [Synchronization](SYNCHRONIZATION.md)
- [History and reports](MIGRATION_HISTORY.md)
- [Playlist sharing](PLAYLIST_SHARING.md)

</div>
</div>

<section class="standard-callout" markdown="1">

## Engine here. Standard there.

This website documents the **Open Playlist Engine implementation**—its runtime,
workflows, safety model, and operations. The canonical format and API reference
remain on the [Open Playlist standard website](https://mbianchidev.github.io/open-playlist/).

[Open the standard website](https://mbianchidev.github.io/open-playlist/){ .button .button-primary }
[View the engine on GitHub](https://github.com/mbianchidev/open-playlist-engine){ .button .button-secondary }

</section>
