PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    rest_id          TEXT NOT NULL DEFAULT '',
    screen_name      TEXT NOT NULL DEFAULT '',
    name             TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL DEFAULT 'user',
    bio              TEXT NOT NULL DEFAULT '',
    location         TEXT NOT NULL DEFAULT '',
    url              TEXT NOT NULL DEFAULT '',
    avatar_url       TEXT NOT NULL DEFAULT '',
    banner_url       TEXT NOT NULL DEFAULT '',
    avatar_file      TEXT NOT NULL DEFAULT '',
    banner_file      TEXT NOT NULL DEFAULT '',
    avatar_source    TEXT NOT NULL DEFAULT '',
    avatar_updated_at INTEGER,
    image_dir        TEXT NOT NULL DEFAULT '',
    last_tweet_id    TEXT NOT NULL DEFAULT '0',
    last_sync_at     INTEGER,
    sync_cursor      TEXT NOT NULL DEFAULT '',
    sync_target_id   TEXT NOT NULL DEFAULT '0',
    sync_max_id      TEXT NOT NULL DEFAULT '0',
    sync_active      INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    protected        INTEGER NOT NULL DEFAULT 0,
    verified         INTEGER NOT NULL DEFAULT 0,
    followers_count  INTEGER NOT NULL DEFAULT 0,
    following_count  INTEGER NOT NULL DEFAULT 0,
    tweet_count      INTEGER NOT NULL DEFAULT 0,
    media_count      INTEGER NOT NULL DEFAULT 0,
    source_path      TEXT NOT NULL DEFAULT '',
    sort_order       INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL DEFAULT 0,
    updated_at       INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_rest_id ON users(rest_id) WHERE rest_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_screen ON users(screen_name COLLATE NOCASE) WHERE screen_name <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_source ON users(source_path) WHERE source_path <> '';

CREATE TABLE IF NOT EXISTS posts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id            TEXT NOT NULL,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    author_id           TEXT NOT NULL DEFAULT '',
    author_name         TEXT NOT NULL DEFAULT '',
    author_screen_name  TEXT NOT NULL DEFAULT '',
    kind                TEXT NOT NULL DEFAULT 'tweet',
    text                TEXT NOT NULL DEFAULT '',
    lang                TEXT NOT NULL DEFAULT '',
    published_at        INTEGER NOT NULL DEFAULT 0,
    collected_at        INTEGER NOT NULL DEFAULT 0,
    favorite_count      INTEGER NOT NULL DEFAULT 0,
    retweet_count       INTEGER NOT NULL DEFAULT 0,
    reply_count         INTEGER NOT NULL DEFAULT 0,
    quote_count         INTEGER NOT NULL DEFAULT 0,
    view_count          INTEGER NOT NULL DEFAULT 0,
    bookmark_count      INTEGER NOT NULL DEFAULT 0,
    is_retweet          INTEGER NOT NULL DEFAULT 0,
    retweet_of_id       TEXT NOT NULL DEFAULT '',
    retweet_of_author   TEXT NOT NULL DEFAULT '',
    is_quote            INTEGER NOT NULL DEFAULT 0,
    quoted_id           TEXT NOT NULL DEFAULT '',
    quoted_author       TEXT NOT NULL DEFAULT '',
    quoted_text         TEXT NOT NULL DEFAULT '',
    quoted_url          TEXT NOT NULL DEFAULT '',
    is_reply            INTEGER NOT NULL DEFAULT 0,
    reply_to_id         TEXT NOT NULL DEFAULT '',
    reply_to_author     TEXT NOT NULL DEFAULT '',
    possibly_sensitive  INTEGER NOT NULL DEFAULT 0,
    source              TEXT NOT NULL DEFAULT '',
    url                 TEXT NOT NULL DEFAULT '',
    is_accessible       INTEGER NOT NULL DEFAULT 1,
    is_deleted          INTEGER NOT NULL DEFAULT 0,
    media_count         INTEGER NOT NULL DEFAULT 0,
    has_video           INTEGER NOT NULL DEFAULT 0,
    search_text         TEXT NOT NULL DEFAULT '',
    UNIQUE(user_id, tweet_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_user_time ON posts(user_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_kind ON posts(kind);

CREATE TABLE IF NOT EXISTS media (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id        INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    media_key      TEXT NOT NULL DEFAULT '',
    media_type     TEXT NOT NULL DEFAULT 'photo',
    url            TEXT NOT NULL DEFAULT '',
    download_url   TEXT NOT NULL DEFAULT '',
    poster_url     TEXT NOT NULL DEFAULT '',
    file_name      TEXT NOT NULL DEFAULT '',
    width          INTEGER NOT NULL DEFAULT 0,
    height         INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    alt_text       TEXT NOT NULL DEFAULT '',
    position       INTEGER NOT NULL DEFAULT 0,
    remote_size    INTEGER,
    local_size     INTEGER,
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    http_status    INTEGER,
    last_error     TEXT,
    downloaded_at  INTEGER,
    UNIQUE(post_id, media_key)
);

CREATE INDEX IF NOT EXISTS idx_media_post ON media(post_id);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);

CREATE TABLE IF NOT EXISTS sync_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    started_at   INTEGER NOT NULL DEFAULT 0,
    finished_at  INTEGER,
    status       TEXT NOT NULL DEFAULT 'running',
    pages        INTEGER NOT NULL DEFAULT 0,
    new_posts    INTEGER NOT NULL DEFAULT 0,
    new_media    INTEGER NOT NULL DEFAULT 0,
    message      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_user ON sync_runs(user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'queued',
    created_at  INTEGER NOT NULL DEFAULT 0,
    started_at  INTEGER,
    finished_at INTEGER,
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    log_path    TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
