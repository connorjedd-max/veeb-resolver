const SESSION_COOKIE = "music_session";
const VEEB_UI_VERSION = "8.0-progressive-reliable";
const SESSION_DAYS = 30;
const PASSWORD_HASH_PREFIX = "hmac1:";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    try {
      if (!env.DB) {
        throw new Error(
          'Missing D1 binding "DB". Add a D1 database binding named exactly DB in Worker Settings -> Bindings.'
        );
      }

      await ensureDatabase(env);
    } catch (error) {
      let startupMessage = "Unknown D1 startup error";

      try {
        if (error && typeof error.message === "string") {
          startupMessage = error.message;
        } else {
          startupMessage = String(error);
        }
      } catch (_) {}

      console.error("Veeb startup error:", startupMessage, error);

      return new Response(
        "VEEB STARTUP ERROR\n\n" + startupMessage,
        {
          status: 500,
          headers: {
            "Content-Type": "text/plain; charset=UTF-8",
            "Cache-Control": "no-store"
          }
        }
      );
    }

    // ============================================================
    // AUTH API
    // ============================================================

    if (url.pathname === "/api/auth/status" && request.method === "GET") {
      const user = await getCurrentUser(request, env);

      return json({
        authenticated: !!user,
        user: user
          ? {
              id: user.id,
              email: user.email
            }
          : null
      });
    }

    if (url.pathname === "/api/auth/register" && request.method === "POST") {
      try {
        return await handleRegister(request, env);
      } catch (error) {
        return apiFailure("register", error);
      }
    }

    if (url.pathname === "/api/auth/login" && request.method === "POST") {
      try {
        return await handleLogin(request, env);
      } catch (error) {
        return apiFailure("login", error);
      }
    }

    if (url.pathname === "/api/auth/logout" && request.method === "POST") {
      try {
        return await handleLogout(request, env);
      } catch (error) {
        return apiFailure("logout", error);
      }
    }

    // ============================================================
    // EVERYTHING BELOW HERE REQUIRES LOGIN
    // ============================================================

    if (url.pathname.startsWith("/api/")) {
      const user = await getCurrentUser(request, env);

      if (!user) {
        return json(
          {
            error: "Unauthorized"
          },
          401
        );
      }

      // ==========================================================
      // SEARCH
      // ==========================================================

      if (url.pathname === "/api/search" && request.method === "GET") {
        const q = url.searchParams.get("q")?.trim();

        if (!q) {
          return json(
            {
              error: "Missing search query"
            },
            400
          );
        }

        /*
          IMPORTANT

          Put your catalogue/search implementation here.

          This example expects searchCatalogue() to return:

          [
            {
              id: "...",
              title: "...",
              artist: "...",
              album: "...",
              artwork: "..."
            }
          ]
        */

        const results = await searchCatalogue(q, env);

        return json(results);
      }

      // ==========================================================
      // RESOLVER WAKE
      // Wake Render as soon as Veeb opens so a first tap does not also pay
      // the service cold-start cost.
      // ==========================================================

      if (url.pathname === "/api/resolver/wake" && request.method === "POST") {
        ctx.waitUntil(
          wakeYouTubeResolver(env).catch(error => {
            console.error("Veeb resolver wake failed:", error);
          })
        );

        return json({ ok: true, status: "waking" }, 202);
      }


      // ==========================================================
      // AUDIO PREFETCH
      // Warms the Render cache without exposing the resolver secret.
      // ==========================================================

      if (
        url.pathname.startsWith("/api/prefetch/") &&
        request.method === "POST"
      ) {
        const trackId = decodeURIComponent(
          url.pathname.slice("/api/prefetch/".length)
        );

        if (!trackId) {
          return json({ error: "Missing track ID" }, 400);
        }

        const intent = url.searchParams.get("intent") === "1";

        ctx.waitUntil(
          prefetchPlayableAudio(trackId, env, intent).catch(error => {
            console.error("Veeb prefetch failed:", trackId, error);
          })
        );

        return json({ ok: true, status: "warming", trackId, intent }, 202);
      }

      // ==========================================================
      // AUDIO STREAM
      // Veeb stays same-origin in the browser. Playback resolution and
      // YouTube media fetching happen on the external resolver so the
      // two upstream requests originate from the same resolver network.
      // ==========================================================

      if (
        url.pathname.startsWith("/api/audio/") &&
        (request.method === "GET" || request.method === "HEAD")
      ) {
        const trackId = decodeURIComponent(
          url.pathname.slice("/api/audio/".length)
        );

        if (!trackId) {
          return new Response("Missing track ID", {
            status: 400,
            headers: {
              "Content-Type": "text/plain; charset=UTF-8",
              "Cache-Control": "no-store"
            }
          });
        }

        try {
          return await streamPlayableAudio(
            request,
            trackId,
            env,
            ctx
          );
        } catch (error) {
          console.error(
            "Veeb audio proxy failed:",
            trackId,
            error
          );

          return new Response(
            "Veeb playback failed: "
            + (
              error && typeof error.message === "string"
                ? error.message
                : String(error)
            ),
            {
              status: 502,
              headers: {
                "Content-Type": "text/plain; charset=UTF-8",
                "Cache-Control": "no-store"
              }
            }
          );
        }
      }

      // ==========================================================
      // PLAYER DIAGNOSTICS
      // Returns resolver metadata without exposing YouTube's temporary
      // media URL. The browser player uses /api/audio/:id.
      // ==========================================================

      if (
        url.pathname.startsWith("/api/play/") &&
        request.method === "GET"
      ) {
        const trackId = decodeURIComponent(
          url.pathname.slice("/api/play/".length)
        );

        if (!trackId) {
          return json(
            {
              error: "Missing track ID"
            },
            400
          );
        }

        /*
          This function is intentionally isolated.

          It should return a media URL you are authorised
          to stream/play.

          Example:
          {
            url: "https://...",
            contentType: "audio/mp4"
          }
        */

        const playable = await getPlayableUrl(trackId, env);

        if (!playable) {
          return json(
            {
              error: "No playable source available"
            },
            404
          );
        }

        return json(playable);
      }

      // ==========================================================
      // RECORD TRACK
      // ==========================================================

      if (url.pathname === "/api/tracks" && request.method === "POST") {
        const track = await request.json();

        await upsertTrack(env, track);

        return json({
          ok: true
        });
      }

      // ==========================================================
      // LISTENING EVENTS
      // ==========================================================

      if (url.pathname === "/api/events" && request.method === "POST") {
        const event = await request.json();

        const outcome = await recordListeningEvent(env, user.id, event);

        if (
          outcome?.needsGenreEnrichment &&
          ctx?.waitUntil
        ) {
          ctx.waitUntil(
            enrichTrackBrainzAndApplySignal(
              env,
              user.id,
              outcome.trackId,
              outcome.delta,
              event.type
            )
          );
        }

        if (["like", "dislike", "complete", "skip"].includes(event.type)) {
          await invalidateRecommendationCache(env, user.id);
        }

        return json({
          ok: true
        });
      }

      // ==========================================================
      // SAVE
      // ==========================================================

      if (url.pathname === "/api/save" && request.method === "POST") {
        const track = await request.json();

        await upsertTrack(env, track);

        await env.DB.prepare(`
          INSERT INTO saved_tracks (
            user_id,
            track_id,
            saved_at
          )
          VALUES (?, ?, unixepoch())

          ON CONFLICT(user_id, track_id)
          DO UPDATE SET
            saved_at = unixepoch()
        `)
          .bind(
            user.id,
            track.id
          )
          .run();

        const saveOutcome = await addPreferenceScore(
          env,
          user.id,
          track.id,
          7,
          "save"
        );

        if (
          saveOutcome?.needsGenreEnrichment &&
          ctx?.waitUntil
        ) {
          ctx.waitUntil(
            enrichTrackBrainzAndApplySignal(
              env,
              user.id,
              track.id,
              7,
              "save"
            )
          );
        }

        await invalidateRecommendationCache(env, user.id);

        return json({
          ok: true
        });
      }

      // ==========================================================
      // UNSAVE
      // ==========================================================

      if (url.pathname === "/api/unsave" && request.method === "POST") {
        const body = await request.json();

        await env.DB.prepare(`
          DELETE FROM saved_tracks
          WHERE user_id = ?
          AND track_id = ?
        `)
          .bind(
            user.id,
            body.trackId
          )
          .run();

        await invalidateRecommendationCache(env, user.id);

        return json({
          ok: true
        });
      }

      // ==========================================================
      // SAVED TRACKS
      // ==========================================================

      if (url.pathname === "/api/saved" && request.method === "GET") {
        const result = await env.DB.prepare(`
          SELECT
            t.id,
            t.title,
            t.artist,
            t.album,
            t.artwork

          FROM saved_tracks s

          INNER JOIN tracks t
            ON t.id = s.track_id

          WHERE s.user_id = ?

          ORDER BY s.saved_at DESC
        `)
          .bind(user.id)
          .all();

        return json(result.results);
      }

      // ==========================================================
      // PLAYLISTS
      // ==========================================================

      if (url.pathname === "/api/playlists" && request.method === "GET") {
        const result = await env.DB.prepare(`
          SELECT
            id,
            name,
            created_at AS createdAt

          FROM playlists

          WHERE user_id = ?

          ORDER BY created_at DESC
        `)
          .bind(user.id)
          .all();

        return json(result.results);
      }

      if (url.pathname === "/api/playlists" && request.method === "POST") {
        const body = await request.json();

        const name = String(body.name || "").trim();

        if (!name) {
          return json(
            {
              error: "Playlist name required"
            },
            400
          );
        }

        const id = crypto.randomUUID();

        await env.DB.prepare(`
          INSERT INTO playlists (
            id,
            user_id,
            name,
            created_at
          )
          VALUES (?, ?, ?, unixepoch())
        `)
          .bind(
            id,
            user.id,
            name
          )
          .run();

        return json({
          id,
          name
        });
      }

      const addPlaylistMatch = url.pathname.match(
        /^\/api\/playlists\/([^/]+)\/tracks$/
      );

      if (
        addPlaylistMatch &&
        request.method === "POST"
      ) {
        const playlistId = decodeURIComponent(
          addPlaylistMatch[1]
        );

        const track = await request.json();

        const playlist = await env.DB.prepare(`
          SELECT id
          FROM playlists
          WHERE id = ?
          AND user_id = ?
        `)
          .bind(
            playlistId,
            user.id
          )
          .first();

        if (!playlist) {
          return json(
            {
              error: "Playlist not found"
            },
            404
          );
        }

        await upsertTrack(env, track);

        const nextPosition = await env.DB.prepare(`
          SELECT
            COALESCE(MAX(position), -1) + 1 AS nextPosition

          FROM playlist_tracks

          WHERE playlist_id = ?
        `)
          .bind(playlistId)
          .first();

        await env.DB.prepare(`
          INSERT INTO playlist_tracks (
            playlist_id,
            track_id,
            position,
            added_at
          )
          VALUES (?, ?, ?, unixepoch())

          ON CONFLICT(playlist_id, track_id)
          DO NOTHING
        `)
          .bind(
            playlistId,
            track.id,
            nextPosition?.nextPosition ?? 0
          )
          .run();

        return json({
          ok: true
        });
      }

      // ==========================================================
      // PLAYLIST TRACKS
      // ==========================================================

      const getPlaylistTracksMatch = url.pathname.match(
        /^\/api\/playlists\/([^/]+)\/tracks$/
      );

      if (
        getPlaylistTracksMatch &&
        request.method === "GET"
      ) {
        const playlistId = decodeURIComponent(
          getPlaylistTracksMatch[1]
        );

        const playlist = await env.DB.prepare(`
          SELECT id, name
          FROM playlists
          WHERE id = ?
            AND user_id = ?
        `)
          .bind(playlistId, user.id)
          .first();

        if (!playlist) {
          return json({ error: "Playlist not found" }, 404);
        }

        const result = await env.DB.prepare(`
          SELECT
            t.id,
            t.title,
            t.artist,
            t.album,
            t.artwork,
            t.provider,
            t.provider_track_id AS providerTrackId
          FROM playlist_tracks pt
          INNER JOIN tracks t
            ON t.id = pt.track_id
          WHERE pt.playlist_id = ?
          ORDER BY pt.position ASC, pt.added_at ASC
        `)
          .bind(playlistId)
          .all();

        return json({
          playlist,
          tracks: result.results || []
        });
      }

      // ==========================================================
      // RADIO / SESSION QUEUE
      // ==========================================================

      if (
        url.pathname.startsWith("/api/radio/") &&
        request.method === "GET"
      ) {
        const seedTrackId = decodeURIComponent(
          url.pathname.slice("/api/radio/".length)
        );

        if (!seedTrackId) {
          return json({ error: "Missing seed track ID" }, 400);
        }

        const tracks = await buildRecommendations(
          env,
          user.id,
          {
            seedTrackId,
            limit: 32,
            useCache: false
          }
        );

        return json(tracks);
      }

      // ==========================================================
      // RECOMMENDATIONS
      // ==========================================================

      if (
        url.pathname === "/api/recommendations" &&
        request.method === "GET"
      ) {
        const tracks = await buildRecommendations(
          env,
          user.id,
          {
            limit: 36,
            useCache: true
          }
        );

        return json(tracks);
      }

      // ==========================================================
      // TASTE PROFILE DEBUG
      // ==========================================================

      if (
        url.pathname === "/api/taste" &&
        request.method === "GET"
      ) {
        const profile = await getTasteProfile(env, user.id);

        return json({
          artists: profile.artists.slice(0, 12),
          genres: profile.genres.slice(0, 12),
          tracks: profile.topTracks.slice(0, 12),
          musicBrainzEnabled: true,
          listenBrainzEnabled: true
        });
      }

      return json(
        {
          error: "API endpoint not found"
        },
        404
      );
    }

    // ============================================================
    // APP
    // ============================================================

    return new Response(APP_HTML, {
      headers: {
        "Content-Type": "text/html; charset=UTF-8",
        "Cache-Control": "no-store"
      }
    });
  },

  // Optional Cloudflare Cron Trigger. If you add `*/10 * * * *` in the
  // Worker dashboard, this keeps the free Render service warm and preserves
  // its in-memory/on-disk session caches instead of losing them after idle.
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      wakeYouTubeResolver(env).catch(error => {
        console.error("Veeb scheduled resolver wake failed:", error);
      })
    );
  }
};


// =================================================================
// AUTH
// =================================================================

async function handleRegister(request, env) {
  const body = await request.json();

  const email = normalizeEmail(body.email);
  const password = String(body.password || "");

  if (!isValidEmail(email)) {
    return json(
      {
        error: "Enter a valid email"
      },
      400
    );
  }

  if (password.length < 10) {
    return json(
      {
        error: "Password must be at least 10 characters"
      },
      400
    );
  }

  const existing = await env.DB.prepare(`
    SELECT id
    FROM users
    WHERE email = ?
  `)
    .bind(email)
    .first();

  if (existing) {
    return json(
      {
        error: "An account already exists with that email"
      },
      409
    );
  }

  const userId = crypto.randomUUID();

  const salt = crypto.getRandomValues(
    new Uint8Array(16)
  );

  const passwordHash = await hashPassword(
    password,
    salt,
    env
  );

  await env.DB.prepare(`
    INSERT INTO users (
      id,
      email,
      password_hash,
      password_salt,
      created_at
    )
    VALUES (?, ?, ?, ?, unixepoch())
  `)
    .bind(
      userId,
      email,
      PASSWORD_HASH_PREFIX + bytesToBase64(passwordHash),
      bytesToBase64(salt)
    )
    .run();

  const session = await createSession(
    env,
    userId
  );

  return jsonWithCookie(
    {
      ok: true,
      user: {
        id: userId,
        email
      }
    },
    session.cookie
  );
}


async function handleLogin(request, env) {
  const body = await request.json();

  const email = normalizeEmail(body.email);
  const password = String(body.password || "");

  const user = await env.DB.prepare(`
    SELECT
      id,
      email,
      password_hash AS passwordHash,
      password_salt AS passwordSalt

    FROM users

    WHERE email = ?
  `)
    .bind(email)
    .first();

  if (!user) {
    return json(
      {
        error: "Incorrect email or password"
      },
      401
    );
  }

  if (
    typeof user.passwordHash !== "string" ||
    !user.passwordHash.startsWith(
      PASSWORD_HASH_PREFIX
    )
  ) {
    throw new Error(
      "This account was created by an older Veeb auth build. Delete that test account from D1 and create it again."
    );
  }

  const expected = base64ToBytes(
    user.passwordHash.slice(
      PASSWORD_HASH_PREFIX.length
    )
  );

  const salt = base64ToBytes(
    user.passwordSalt
  );

  const actual = await hashPassword(
    password,
    salt,
    env
  );

  const matches = timingSafeBytesEqual(
    expected,
    actual
  );

  if (!matches) {
    return json(
      {
        error: "Incorrect email or password"
      },
      401
    );
  }

  const session = await createSession(
    env,
    user.id
  );

  return jsonWithCookie(
    {
      ok: true,
      user: {
        id: user.id,
        email: user.email
      }
    },
    session.cookie
  );
}


async function handleLogout(request, env) {
  const token = getCookie(
    request,
    SESSION_COOKIE
  );

  if (token) {
    const tokenHash = await sha256Hex(token);

    await env.DB.prepare(`
      DELETE FROM sessions
      WHERE token_hash = ?
    `)
      .bind(tokenHash)
      .run();
  }

  return jsonWithCookie(
    {
      ok: true
    },
    `${SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`
  );
}


async function createSession(env, userId) {
  const random = crypto.getRandomValues(
    new Uint8Array(32)
  );

  const token = bytesToBase64Url(random);

  const tokenHash = await sha256Hex(token);

  const expiresAt =
    Math.floor(Date.now() / 1000) +
    SESSION_DAYS * 24 * 60 * 60;

  await env.DB.prepare(`
    INSERT INTO sessions (
      id,
      user_id,
      token_hash,
      expires_at,
      created_at
    )
    VALUES (?, ?, ?, ?, unixepoch())
  `)
    .bind(
      crypto.randomUUID(),
      userId,
      tokenHash,
      expiresAt
    )
    .run();

  const cookie =
    `${SESSION_COOKIE}=${token}; ` +
    `Path=/; ` +
    `HttpOnly; ` +
    `Secure; ` +
    `SameSite=Lax; ` +
    `Max-Age=${SESSION_DAYS * 24 * 60 * 60}`;

  return {
    token,
    cookie
  };
}


async function getCurrentUser(request, env) {
  const token = getCookie(
    request,
    SESSION_COOKIE
  );

  if (!token) {
    return null;
  }

  const tokenHash = await sha256Hex(token);

  const user = await env.DB.prepare(`
    SELECT
      u.id,
      u.email

    FROM sessions s

    INNER JOIN users u
      ON u.id = s.user_id

    WHERE
      s.token_hash = ?
      AND s.expires_at > unixepoch()

    LIMIT 1
  `)
    .bind(tokenHash)
    .first();

  return user || null;
}


async function hashPassword(password, salt, env) {
  if (!env.AUTH_PEPPER) {
    throw new Error(
      'Missing AUTH_PEPPER Worker secret. Add a secret named exactly AUTH_PEPPER in Worker Settings -> Variables and Secrets.'
    );
  }

  const encoder = new TextEncoder();

  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(
      String(env.AUTH_PEPPER)
    ),
    {
      name: "HMAC",
      hash: "SHA-256"
    },
    false,
    ["sign"]
  );

  const passwordBytes =
    encoder.encode(password);

  const payload =
    new Uint8Array(
      salt.length + passwordBytes.length
    );

  payload.set(salt, 0);
  payload.set(
    passwordBytes,
    salt.length
  );

  const signature =
    await crypto.subtle.sign(
      "HMAC",
      key,
      payload
    );

  return new Uint8Array(
    signature
  );
}

function timingSafeBytesEqual(a, b) {
  if (a.length !== b.length) {
    return false;
  }

  if (
    typeof crypto.subtle.timingSafeEqual === "function"
  ) {
    return crypto.subtle.timingSafeEqual(
      a,
      b
    );
  }

  let diff = 0;

  for (let i = 0; i < a.length; i++) {
    diff |= a[i] ^ b[i];
  }

  return diff === 0;
}


// =================================================================
// DATABASE
// =================================================================

let schemaReadyPromise = null;

async function ensureDatabase(env) {
  if (!schemaReadyPromise) {
    schemaReadyPromise = initialiseDatabaseSchema(env).catch(error => {
      schemaReadyPromise = null;
      throw error;
    });
  }

  return schemaReadyPromise;
}


async function initialiseDatabaseSchema(env) {
  const steps = [
    {
      name: "users table",
      sql: `
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          password_salt TEXT NOT NULL,
          created_at INTEGER NOT NULL DEFAULT 0
        )
      `
    },
    {
      name: "sessions table",
      sql: `
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          expires_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL DEFAULT 0
        )
      `
    },
    {
      name: "sessions token index",
      sql: `
        CREATE INDEX IF NOT EXISTS idx_sessions_token
        ON sessions(token_hash)
      `
    },
    {
      name: "sessions user index",
      sql: `
        CREATE INDEX IF NOT EXISTS idx_sessions_user
        ON sessions(user_id)
      `
    },
    {
      name: "tracks table",
      sql: `
        CREATE TABLE IF NOT EXISTS tracks (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          artist TEXT,
          album TEXT,
          artwork TEXT,
          created_at INTEGER NOT NULL DEFAULT 0
        )
      `
    },
    {
      name: "saved tracks table",
      sql: `
        CREATE TABLE IF NOT EXISTS saved_tracks (
          user_id TEXT NOT NULL,
          track_id TEXT NOT NULL,
          saved_at INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (user_id, track_id)
        )
      `
    },
    {
      name: "playlists table",
      sql: `
        CREATE TABLE IF NOT EXISTS playlists (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          name TEXT NOT NULL,
          created_at INTEGER NOT NULL DEFAULT 0
        )
      `
    },
    {
      name: "playlist tracks table",
      sql: `
        CREATE TABLE IF NOT EXISTS playlist_tracks (
          playlist_id TEXT NOT NULL,
          track_id TEXT NOT NULL,
          position INTEGER NOT NULL,
          added_at INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (playlist_id, track_id)
        )
      `
    },
    {
      name: "listening events table",
      sql: `
        CREATE TABLE IF NOT EXISTS listening_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL,
          track_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          position_seconds REAL,
          duration_seconds REAL,
          created_at INTEGER NOT NULL DEFAULT 0
        )
      `
    },
    {
      name: "track preferences table",
      sql: `
        CREATE TABLE IF NOT EXISTS track_preferences (
          user_id TEXT NOT NULL,
          track_id TEXT NOT NULL,
          score REAL NOT NULL DEFAULT 0,
          play_count INTEGER NOT NULL DEFAULT 0,
          completion_count INTEGER NOT NULL DEFAULT 0,
          skip_count INTEGER NOT NULL DEFAULT 0,
          like_count INTEGER NOT NULL DEFAULT 0,
          dislike_count INTEGER NOT NULL DEFAULT 0,
          save_count INTEGER NOT NULL DEFAULT 0,
          last_played_at INTEGER,
          PRIMARY KEY (user_id, track_id)
        )
      `
    },
    {
      name: "artist preferences table",
      sql: `
        CREATE TABLE IF NOT EXISTS artist_preferences (
          user_id TEXT NOT NULL,
          artist_key TEXT NOT NULL,
          artist_name TEXT NOT NULL,
          artist_id TEXT,
          score REAL NOT NULL DEFAULT 0,
          play_count INTEGER NOT NULL DEFAULT 0,
          completion_count INTEGER NOT NULL DEFAULT 0,
          skip_count INTEGER NOT NULL DEFAULT 0,
          like_count INTEGER NOT NULL DEFAULT 0,
          dislike_count INTEGER NOT NULL DEFAULT 0,
          save_count INTEGER NOT NULL DEFAULT 0,
          last_played_at INTEGER,
          PRIMARY KEY (user_id, artist_key)
        )
      `
    },
    {
      name: "genre preferences table",
      sql: `
        CREATE TABLE IF NOT EXISTS genre_preferences (
          user_id TEXT NOT NULL,
          genre TEXT NOT NULL,
          score REAL NOT NULL DEFAULT 0,
          play_count INTEGER NOT NULL DEFAULT 0,
          completion_count INTEGER NOT NULL DEFAULT 0,
          skip_count INTEGER NOT NULL DEFAULT 0,
          like_count INTEGER NOT NULL DEFAULT 0,
          dislike_count INTEGER NOT NULL DEFAULT 0,
          save_count INTEGER NOT NULL DEFAULT 0,
          last_played_at INTEGER,
          PRIMARY KEY (user_id, genre)
        )
      `
    },
    {
      name: "recommendation cache table",
      sql: `
        CREATE TABLE IF NOT EXISTS recommendation_cache (
          user_id TEXT PRIMARY KEY,
          payload TEXT NOT NULL,
          generated_at INTEGER NOT NULL DEFAULT 0
        )
      `
    },
    {
      name: "events user time index",
      sql: `
        CREATE INDEX IF NOT EXISTS idx_events_user_time
        ON listening_events(user_id, created_at DESC)
      `
    },
    {
      name: "track preferences score index",
      sql: `
        CREATE INDEX IF NOT EXISTS idx_track_prefs_user_score
        ON track_preferences(user_id, score DESC)
      `
    },
    {
      name: "artist preferences score index",
      sql: `
        CREATE INDEX IF NOT EXISTS idx_artist_prefs_user_score
        ON artist_preferences(user_id, score DESC)
      `
    },
    {
      name: "genre preferences score index",
      sql: `
        CREATE INDEX IF NOT EXISTS idx_genre_prefs_user_score
        ON genre_preferences(user_id, score DESC)
      `
    }
  ];

  for (const step of steps) {
    try {
      await env.DB.prepare(step.sql).run();
    } catch (error) {
      const message =
        error && typeof error.message === "string"
          ? error.message
          : String(error);

      throw new Error(
        `D1 schema step failed (${step.name}): ${message}`
      );
    }
  }

  await ensureColumn(env, "tracks", "artist_id", "TEXT");
  await ensureColumn(env, "tracks", "album_id", "TEXT");
  await ensureColumn(env, "tracks", "duration_seconds", "INTEGER");
  await ensureColumn(env, "tracks", "canonical_key", "TEXT");
  await ensureColumn(env, "tracks", "genres_json", "TEXT");
  await ensureColumn(env, "tracks", "video_type", "TEXT");
  await ensureColumn(env, "tracks", "source_quality", "REAL NOT NULL DEFAULT 0");
  await ensureColumn(env, "tracks", "last_enriched_at", "INTEGER NOT NULL DEFAULT 0");
  await ensureColumn(env, "tracks", "musicbrainz_recording_id", "TEXT");
  await ensureColumn(env, "tracks", "musicbrainz_artist_id", "TEXT");
  await ensureColumn(env, "tracks", "brainz_match_score", "REAL NOT NULL DEFAULT 0");
  await ensureColumn(env, "tracks", "brainz_last_attempt_at", "INTEGER NOT NULL DEFAULT 0");
  await ensureColumn(env, "artist_preferences", "musicbrainz_artist_id", "TEXT");
}


async function ensureColumn(env, tableName, columnName, definition) {
  const result = await env.DB.prepare(
    "PRAGMA table_info(" + tableName + ")"
  ).all();

  const exists = (result.results || []).some(
    row => row.name === columnName
  );

  if (exists) {
    return;
  }

  await env.DB.prepare(
    "ALTER TABLE " + tableName + " ADD COLUMN " + columnName + " " + definition
  ).run();
}


async function upsertTrack(env, track) {
  const canonicalKey =
    track.canonicalKey ||
    makeCanonicalKey(track.artist, track.title);

  const genresJson =
    Array.isArray(track.genres)
      ? JSON.stringify(track.genres)
      : track.genresJson || null;

  await env.DB.prepare(`
    INSERT INTO tracks (
      id,
      title,
      artist,
      album,
      artwork,
      artist_id,
      album_id,
      duration_seconds,
      canonical_key,
      genres_json,
      video_type,
      source_quality,
      musicbrainz_recording_id,
      musicbrainz_artist_id,
      brainz_match_score,
      created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())

    ON CONFLICT(id)
    DO UPDATE SET
      title = excluded.title,
      artist = excluded.artist,
      album = excluded.album,
      artwork = excluded.artwork,
      artist_id = COALESCE(excluded.artist_id, tracks.artist_id),
      album_id = COALESCE(excluded.album_id, tracks.album_id),
      duration_seconds = COALESCE(excluded.duration_seconds, tracks.duration_seconds),
      canonical_key = COALESCE(excluded.canonical_key, tracks.canonical_key),
      genres_json = COALESCE(excluded.genres_json, tracks.genres_json),
      video_type = COALESCE(excluded.video_type, tracks.video_type),
      source_quality = MAX(COALESCE(tracks.source_quality, 0), COALESCE(excluded.source_quality, 0)),
      musicbrainz_recording_id = COALESCE(excluded.musicbrainz_recording_id, tracks.musicbrainz_recording_id),
      musicbrainz_artist_id = COALESCE(excluded.musicbrainz_artist_id, tracks.musicbrainz_artist_id),
      brainz_match_score = MAX(COALESCE(tracks.brainz_match_score, 0), COALESCE(excluded.brainz_match_score, 0))
  `)
    .bind(
      String(track.id),
      String(track.title || "Unknown"),
      track.artist || null,
      track.album || null,
      track.artwork || null,
      track.artistId || null,
      track.albumId || null,
      Number(track.durationSeconds || 0) || null,
      canonicalKey || null,
      genresJson,
      track.videoType || null,
      Number(track.sourceQuality || 0),
      track.musicBrainzRecordingId || null,
      track.musicBrainzArtistId || null,
      Number(track.brainzMatchScore || 0)
    )
    .run();
}


async function recordListeningEvent(
  env,
  userId,
  event
) {
  const trackId = String(event.trackId || "");

  if (!trackId) {
    return null;
  }

  const type = String(event.type || "");
  const position = Number(event.positionSeconds) || 0;
  const duration = Number(event.durationSeconds) || 0;
  const delta = getListeningSignalDelta(type, position, duration);

  await env.DB.prepare(`
    INSERT INTO listening_events (
      user_id,
      track_id,
      event_type,
      position_seconds,
      duration_seconds,
      created_at
    )
    VALUES (?, ?, ?, ?, ?, unixepoch())
  `)
    .bind(userId, trackId, type, position, duration)
    .run();

  await env.DB.prepare(`
    INSERT INTO track_preferences (
      user_id,
      track_id,
      score,
      play_count,
      completion_count,
      skip_count,
      like_count,
      dislike_count,
      save_count,
      last_played_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, unixepoch())

    ON CONFLICT(user_id, track_id)
    DO UPDATE SET
      score = track_preferences.score + excluded.score,
      play_count = track_preferences.play_count + excluded.play_count,
      completion_count = track_preferences.completion_count + excluded.completion_count,
      skip_count = track_preferences.skip_count + excluded.skip_count,
      like_count = track_preferences.like_count + excluded.like_count,
      dislike_count = track_preferences.dislike_count + excluded.dislike_count,
      last_played_at = unixepoch()
  `)
    .bind(
      userId,
      trackId,
      delta,
      type === "play" ? 1 : 0,
      type === "complete" ? 1 : 0,
      type === "skip" ? 1 : 0,
      type === "like" ? 1 : 0,
      type === "dislike" ? 1 : 0
    )
    .run();

  const track = await getStoredTrack(env, trackId);

  if (track?.artist) {
    await updateArtistPreference(
      env,
      userId,
      track,
      delta * 0.55,
      type
    );
  }

  const genres = parseStoredGenres(track?.genresJson);

  if (genres.length) {
    await updateGenrePreferences(
      env,
      userId,
      genres,
      delta * 0.38,
      type
    );
  }

  return {
    trackId,
    delta,
    needsGenreEnrichment:
      !!track?.artist &&
      (
        !genres.length ||
        !track.musicBrainzRecordingId ||
        !track.musicBrainzArtistId
      ) &&
      ["play", "complete", "like", "dislike"].includes(type)
  };
}


function getListeningSignalDelta(type, position, duration) {
  if (type === "play") {
    return 0.15;
  }

  if (type === "complete") {
    return 3.25;
  }

  if (type === "like") {
    return 9;
  }

  if (type === "dislike") {
    return -16;
  }

  if (type === "save") {
    return 7;
  }

  if (type === "skip") {
    const progress =
      duration > 0
        ? position / duration
        : 0;

    if (progress < 0.10) return -6;
    if (progress < 0.30) return -4;
    if (progress < 0.65) return -1.5;
    return -0.25;
  }

  return 0;
}


async function addPreferenceScore(
  env,
  userId,
  trackId,
  amount,
  type
) {
  await env.DB.prepare(`
    INSERT INTO track_preferences (
      user_id,
      track_id,
      score,
      save_count
    )
    VALUES (?, ?, ?, ?)

    ON CONFLICT(user_id, track_id)
    DO UPDATE SET
      score = score + excluded.score,
      save_count = save_count + excluded.save_count
  `)
    .bind(
      userId,
      trackId,
      amount,
      type === "save" ? 1 : 0
    )
    .run();

  const track = await getStoredTrack(env, trackId);

  if (track?.artist) {
    await updateArtistPreference(
      env,
      userId,
      track,
      amount * 0.55,
      type
    );
  }

  const genres = parseStoredGenres(track?.genresJson);

  if (genres.length) {
    await updateGenrePreferences(
      env,
      userId,
      genres,
      amount * 0.38,
      type
    );
  }

  return {
    needsGenreEnrichment:
      !!track?.artist &&
      (
        !genres.length ||
        !track.musicBrainzRecordingId ||
        !track.musicBrainzArtistId
      )
  };
}


async function getStoredTrack(env, trackId) {
  return env.DB.prepare(`
    SELECT
      id,
      title,
      artist,
      album,
      artwork,
      artist_id AS artistId,
      album_id AS albumId,
      duration_seconds AS durationSeconds,
      canonical_key AS canonicalKey,
      genres_json AS genresJson,
      video_type AS videoType,
      source_quality AS sourceQuality,
      last_enriched_at AS lastEnrichedAt,
      musicbrainz_recording_id AS musicBrainzRecordingId,
      musicbrainz_artist_id AS musicBrainzArtistId,
      brainz_match_score AS brainzMatchScore,
      brainz_last_attempt_at AS brainzLastAttemptAt
    FROM tracks
    WHERE id = ?
  `)
    .bind(trackId)
    .first();
}


async function updateArtistPreference(
  env,
  userId,
  track,
  delta,
  type
) {
  const artistKey = normalizeArtistKey(track.artist);

  if (!artistKey) {
    return;
  }

  await env.DB.prepare(`
    INSERT INTO artist_preferences (
      user_id,
      artist_key,
      artist_name,
      artist_id,
      musicbrainz_artist_id,
      score,
      play_count,
      completion_count,
      skip_count,
      like_count,
      dislike_count,
      save_count,
      last_played_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())

    ON CONFLICT(user_id, artist_key)
    DO UPDATE SET
      artist_name = excluded.artist_name,
      artist_id = COALESCE(excluded.artist_id, artist_preferences.artist_id),
      musicbrainz_artist_id = COALESCE(excluded.musicbrainz_artist_id, artist_preferences.musicbrainz_artist_id),
      score = artist_preferences.score + excluded.score,
      play_count = artist_preferences.play_count + excluded.play_count,
      completion_count = artist_preferences.completion_count + excluded.completion_count,
      skip_count = artist_preferences.skip_count + excluded.skip_count,
      like_count = artist_preferences.like_count + excluded.like_count,
      dislike_count = artist_preferences.dislike_count + excluded.dislike_count,
      save_count = artist_preferences.save_count + excluded.save_count,
      last_played_at = unixepoch()
  `)
    .bind(
      userId,
      artistKey,
      track.artist,
      track.artistId || null,
      track.musicBrainzArtistId || null,
      delta,
      type === "play" ? 1 : 0,
      type === "complete" ? 1 : 0,
      type === "skip" ? 1 : 0,
      type === "like" ? 1 : 0,
      type === "dislike" ? 1 : 0,
      type === "save" ? 1 : 0
    )
    .run();
}


async function updateGenrePreferences(
  env,
  userId,
  genres,
  delta,
  type
) {
  for (const genre of genres.slice(0, 6)) {
    const name = String(genre.name || genre).trim().toLowerCase();
    const weight = Number(genre.weight || 1);

    if (!name) continue;

    await env.DB.prepare(`
      INSERT INTO genre_preferences (
        user_id,
        genre,
        score,
        play_count,
        completion_count,
        skip_count,
        like_count,
        dislike_count,
        save_count,
        last_played_at
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())

      ON CONFLICT(user_id, genre)
      DO UPDATE SET
        score = genre_preferences.score + excluded.score,
        play_count = genre_preferences.play_count + excluded.play_count,
        completion_count = genre_preferences.completion_count + excluded.completion_count,
        skip_count = genre_preferences.skip_count + excluded.skip_count,
        like_count = genre_preferences.like_count + excluded.like_count,
        dislike_count = genre_preferences.dislike_count + excluded.dislike_count,
        save_count = genre_preferences.save_count + excluded.save_count,
        last_played_at = unixepoch()
    `)
      .bind(
        userId,
        name,
        delta * weight,
        type === "play" ? 1 : 0,
        type === "complete" ? 1 : 0,
        type === "skip" ? 1 : 0,
        type === "like" ? 1 : 0,
        type === "dislike" ? 1 : 0,
        type === "save" ? 1 : 0
      )
      .run();
  }
}


function parseStoredGenres(value) {
  if (!value) return [];

  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}


// =================================================================
// CATALOGUE / PLAYBACK PROVIDER
// =================================================================

const YT_INNERTUBE_API_KEY =
  "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30";

const YTM_BASE_API =
  "https://music.youtube.com/youtubei/v1/";

const YTM_SONG_SEARCH_PARAMS =
  "EgWKAQIIAWoMEA4QChADEAQQCRAF";

let ytmVisitorPromise = null;


async function searchCatalogue(query, env) {
  try {
    const tracks = await ytmSearchSongs(query, 30);

    if (tracks.length) {
      return tracks.slice(0, 24);
    }
  } catch (error) {
    console.error("YTM song search failed. Falling back to YouTube Data API.", error);
  }

  return searchYouTubeFallback(query, env);
}


async function ytmSearchSongs(query, limit = 20) {
  const response = await ytmRequest(
    "search",
    {
      query,
      params: YTM_SONG_SEARCH_PARAMS
    }
  );

  const renderers = [];
  collectObjectsByKey(
    response,
    "musicResponsiveListItemRenderer",
    renderers
  );

  const tracks = renderers
    .map(extractYtmSearchTrack)
    .filter(Boolean);

  return cleanAndRankSongResults(
    tracks,
    query,
    limit
  );
}


async function ytmGetRadio(videoId, limit = 40) {
  const response = await ytmRequest(
    "next",
    {
      enablePersistentPlaylistPanel: true,
      isAudioOnly: true,
      tunerSettingValue: "AUTOMIX_SETTING_NORMAL",
      videoId,
      playlistId: "RDAMVM" + videoId,
      params: "wAEB"
    }
  );

  const renderers = [];
  collectObjectsByKey(
    response,
    "playlistPanelVideoRenderer",
    renderers
  );

  const tracks = renderers
    .map(extractYtmRadioTrack)
    .filter(Boolean)
    .filter(track => track.id !== videoId);

  return cleanAndRankSongResults(
    tracks,
    "",
    limit,
    true
  );
}


async function ytmRequest(endpoint, body) {
  const visitorData = await getYtmVisitorData();
  const clientVersion = getYtmClientVersion();

  const payload = {
    ...body,
    context: {
      client: {
        clientName: "WEB_REMIX",
        clientVersion,
        hl: "en",
        gl: "AU"
      },
      user: {}
    }
  };

  const headers = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://music.youtube.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
  };

  if (visitorData) {
    headers["X-Goog-Visitor-Id"] = visitorData;
  }

  const response = await fetch(
    YTM_BASE_API + endpoint +
      "?alt=json&key=" + encodeURIComponent(YT_INNERTUBE_API_KEY),
    {
      method: "POST",
      headers,
      body: JSON.stringify(payload)
    }
  );

  const text = await response.text();
  let data = null;

  try {
    data = JSON.parse(text);
  } catch {
    throw new Error(
      "YouTube Music returned non-JSON data for " + endpoint
    );
  }

  if (!response.ok) {
    const message =
      data?.error?.message ||
      "HTTP " + response.status;

    throw new Error(
      "YouTube Music " + endpoint + " failed: " + message
    );
  }

  return data;
}


async function getYtmVisitorData() {
  if (!ytmVisitorPromise) {
    ytmVisitorPromise = fetch(
      "https://music.youtube.com/",
      {
        headers: {
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
        }
      }
    )
      .then(response => response.text())
      .then(text => {
        const direct = text.match(
          /"VISITOR_DATA"\s*:\s*"([^"]+)"/
        );

        if (direct?.[1]) {
          return direct[1];
        }

        const escaped = text.match(
          /VISITOR_DATA\\?"\s*:\s*\\?"([^"\\]+(?:\\.[^"\\]*)*)/
        );

        return escaped?.[1]
          ? escaped[1].replaceAll("\\u003d", "=")
          : "";
      })
      .catch(() => "");
  }

  return ytmVisitorPromise;
}


function getYtmClientVersion() {
  const day = new Date()
    .toISOString()
    .slice(0, 10)
    .replaceAll("-", "");

  return "1." + day + ".01.00";
}


function collectObjectsByKey(value, key, output) {
  if (!value || typeof value !== "object") {
    return;
  }

  if (Object.prototype.hasOwnProperty.call(value, key)) {
    const candidate = value[key];

    if (candidate && typeof candidate === "object") {
      output.push(candidate);
    }
  }

  if (Array.isArray(value)) {
    for (const item of value) {
      collectObjectsByKey(item, key, output);
    }
    return;
  }

  for (const child of Object.values(value)) {
    collectObjectsByKey(child, key, output);
  }
}


function deepFindFirst(value, predicate) {
  if (!value || typeof value !== "object") {
    return null;
  }

  if (predicate(value)) {
    return value;
  }

  const children = Array.isArray(value)
    ? value
    : Object.values(value);

  for (const child of children) {
    const found = deepFindFirst(child, predicate);
    if (found) return found;
  }

  return null;
}


function extractYtmSearchTrack(renderer) {
  const flexColumns = Array.isArray(renderer.flexColumns)
    ? renderer.flexColumns
    : [];

  const titleRuns =
    flexColumns[0]?.musicResponsiveListItemFlexColumnRenderer?.text?.runs || [];

  const title = titleRuns[0]?.text || "";

  const watchNode = deepFindFirst(
    renderer,
    node => !!node.watchEndpoint?.videoId
  );

  const videoId =
    watchNode?.watchEndpoint?.videoId ||
    renderer.playlistItemData?.videoId ||
    "";

  if (!videoId || !title) {
    return null;
  }

  const metaRuns = [];

  for (const column of flexColumns.slice(1)) {
    const runs =
      column?.musicResponsiveListItemFlexColumnRenderer?.text?.runs || [];
    metaRuns.push(...runs);
  }

  const fixedRuns =
    renderer.fixedColumns?.[0]?.musicResponsiveListItemFixedColumnRenderer?.text?.runs || [];

  const meta = extractArtistAlbumFromRuns(metaRuns);
  const durationText =
    fixedRuns.find(run => /^\d{1,2}:\d{2}(?::\d{2})?$/.test(run.text || ""))?.text ||
    metaRuns.find(run => /^\d{1,2}:\d{2}(?::\d{2})?$/.test(run.text || ""))?.text ||
    "";

  const thumbs =
    renderer.thumbnail?.musicThumbnailRenderer?.thumbnail?.thumbnails || [];

  const videoTypeNode = deepFindFirst(
    renderer,
    node => typeof node.musicVideoType === "string"
  );

  const videoType = videoTypeNode?.musicVideoType || "";

  const track = {
    id: videoId,
    title,
    artist: meta.artist || "Unknown artist",
    artistId: meta.artistId || null,
    album: meta.album || null,
    albumId: meta.albumId || null,
    artwork: thumbs.length ? thumbs[thumbs.length - 1].url : null,
    durationSeconds: parseDurationSeconds(durationText),
    videoType,
    sourceQuality: 0
  };

  track.canonicalKey = makeCanonicalKey(track.artist, track.title);
  track.sourceQuality = scoreSourceQuality(track);

  return track;
}


function extractYtmRadioTrack(renderer) {
  const videoId = renderer.videoId || "";
  const title = renderer.title?.runs?.[0]?.text || "";

  if (!videoId || !title || renderer.unplayableText) {
    return null;
  }

  const metaRuns = renderer.longBylineText?.runs || [];
  const meta = extractArtistAlbumFromRuns(metaRuns);
  const thumbs = renderer.thumbnail?.thumbnails || [];

  const videoTypeNode = deepFindFirst(
    renderer,
    node => typeof node.musicVideoType === "string"
  );

  const track = {
    id: videoId,
    title,
    artist: meta.artist || "Unknown artist",
    artistId: meta.artistId || null,
    album: meta.album || null,
    albumId: meta.albumId || null,
    artwork: thumbs.length ? thumbs[thumbs.length - 1].url : null,
    durationSeconds: parseDurationSeconds(renderer.lengthText?.runs?.[0]?.text || ""),
    videoType: videoTypeNode?.musicVideoType || "",
    sourceQuality: 0
  };

  track.canonicalKey = makeCanonicalKey(track.artist, track.title);
  track.sourceQuality = scoreSourceQuality(track);

  return track;
}


function extractArtistAlbumFromRuns(runs) {
  const artists = [];
  let artistId = null;
  let album = null;
  let albumId = null;

  for (const run of runs || []) {
    const text = String(run?.text || "").trim();
    const browse = run?.navigationEndpoint?.browseEndpoint;
    const browseId = browse?.browseId || "";
    const pageType =
      browse?.browseEndpointContextSupportedConfigs
        ?.browseEndpointContextMusicConfig
        ?.pageType || "";

    if (!text || text === " • ") {
      continue;
    }

    if (
      pageType.includes("ARTIST") ||
      browseId.startsWith("UC")
    ) {
      if (!artists.includes(text)) {
        artists.push(text);
      }
      artistId ||= browseId || null;
      continue;
    }

    if (
      pageType.includes("ALBUM") ||
      browseId.startsWith("MPRE")
    ) {
      album ||= text;
      albumId ||= browseId || null;
    }
  }

  if (!artists.length) {
    for (const run of runs || []) {
      const text = String(run?.text || "").trim();

      if (
        !text ||
        text === "•" ||
        /^\d{1,2}:\d{2}/.test(text) ||
        /^\d{4}$/.test(text) ||
        /^(song|songs|single|album|ep)$/i.test(text)
      ) {
        continue;
      }

      if (!run?.navigationEndpoint?.browseEndpoint) {
        continue;
      }

      artists.push(text);
      break;
    }
  }

  return {
    artist: artists.join(", "),
    artistId,
    album,
    albumId
  };
}


function cleanAndRankSongResults(
  tracks,
  query,
  limit,
  radioMode = false
) {
  const bestByKey = new Map();

  for (const rawTrack of tracks) {
    const track = {
      ...rawTrack
    };

    const penalty = getVariantPenalty(track, query);

    if (penalty >= 100) {
      continue;
    }

    const canonicalKey =
      track.canonicalKey ||
      makeCanonicalKey(track.artist, track.title);

    if (!canonicalKey) {
      continue;
    }

    track.canonicalKey = canonicalKey;
    track.sourceQuality =
      Number(track.sourceQuality || 0) - penalty;

    if (
      !radioMode &&
      track.videoType &&
      track.videoType.includes("OMV")
    ) {
      continue;
    }

    const existing = bestByKey.get(canonicalKey);

    if (
      !existing ||
      track.sourceQuality > existing.sourceQuality
    ) {
      bestByKey.set(canonicalKey, track);
    }
  }

  return [...bestByKey.values()]
    .sort(
      (a, b) =>
        Number(b.sourceQuality || 0) -
        Number(a.sourceQuality || 0)
    )
    .slice(0, limit);
}


function scoreSourceQuality(track) {
  let score = 0;

  if (track.videoType?.includes("ATV")) score += 12;
  if (track.album) score += 4;
  if (track.artistId) score += 2;
  if (track.durationSeconds > 45) score += 1;

  const title = String(track.title || "").toLowerCase();

  if (title.includes("official audio")) score += 1;
  if (title.includes("official video")) score -= 10;
  if (title.includes("music video")) score -= 10;

  return score;
}


function getVariantPenalty(track, query) {
  const title = String(track.title || "").toLowerCase();
  const q = String(query || "").toLowerCase();

  const checks = [
    {
      wanted: /\blive\b/.test(q),
      pattern: /(\(|\[|\-|•)\s*live\b|\blive\s+(at|from|on|in)\b|\bin concert\b/i,
      penalty: 100
    },
    {
      wanted: /\bacoustic\b/.test(q),
      pattern: /\bacoustic\b/i,
      penalty: 100
    },
    {
      wanted: /\bremix\b/.test(q),
      pattern: /\bremix\b/i,
      penalty: 100
    },
    {
      wanted: /\bcover\b/.test(q),
      pattern: /\bcover\b/i,
      penalty: 100
    },
    {
      wanted: /\binstrumental\b/.test(q),
      pattern: /\binstrumental\b/i,
      penalty: 100
    },
    {
      wanted: /\bkaraoke\b/.test(q),
      pattern: /\bkaraoke\b/i,
      penalty: 100
    },
    {
      wanted: /\bslowed\b/.test(q),
      pattern: /\bslowed\b|\breverb\b/i,
      penalty: 100
    },
    {
      wanted: /\bsped\b/.test(q),
      pattern: /\bsped\s*up\b|\bnightcore\b/i,
      penalty: 100
    }
  ];

  for (const check of checks) {
    if (!check.wanted && check.pattern.test(title)) {
      return check.penalty;
    }
  }

  if (/\b(official music video|music video)\b/i.test(title)) {
    return 60;
  }

  if (/\b(session|performance)\b/i.test(title) && !/\b(session|performance)\b/i.test(q)) {
    return 25;
  }

  return 0;
}


function makeCanonicalKey(artist, title) {
  const a = normalizeArtistKey(artist);
  const t = normalizeTitleKey(title);

  if (!t) return "";

  return a + "::" + t;
}


function normalizeArtistKey(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\bvevo\b/g, "")
    .replace(/\s*-\s*topic\s*$/i, "")
    .replace(/\bofficial\b/g, "")
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}


function normalizeTitleKey(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\[(official audio|official video|music video|lyrics?|visuali[sz]er)\]/gi, " ")
    .replace(/\((official audio|official video|music video|lyrics?|visuali[sz]er)\)/gi, " ")
    .replace(/\((?:\d{4}\s*)?remaster(?:ed)?[^)]*\)/gi, " ")
    .replace(/\[(?:\d{4}\s*)?remaster(?:ed)?[^\]]*\]/gi, " ")
    .replace(/\bremaster(?:ed)?\s*\d{0,4}\b/gi, " ")
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}


function parseDurationSeconds(value) {
  const bits = String(value || "")
    .split(":")
    .map(Number);

  if (!bits.length || bits.some(Number.isNaN)) {
    return 0;
  }

  let seconds = 0;

  for (const bit of bits) {
    seconds = seconds * 60 + bit;
  }

  return seconds;
}


async function searchYouTubeFallback(query, env) {
  if (!env.YOUTUBE_API_KEY) {
    return [];
  }

  const url = new URL(
    "https://www.googleapis.com/youtube/v3/search"
  );

  url.searchParams.set("part", "snippet");
  url.searchParams.set("type", "video");
  url.searchParams.set("videoCategoryId", "10");
  url.searchParams.set("maxResults", "30");
  url.searchParams.set("q", query + " official audio");
  url.searchParams.set("key", env.YOUTUBE_API_KEY);

  const response = await fetch(url.toString());

  if (!response.ok) {
    return [];
  }

  const data = await response.json();

  const tracks = (data.items || [])
    .filter(item => item?.id?.videoId)
    .map(item => {
      const snippet = item.snippet || {};
      const track = {
        id: item.id.videoId,
        title: decodeYouTubeText(snippet.title || "Unknown track"),
        artist: cleanFallbackChannelName(
          decodeYouTubeText(snippet.channelTitle || "Unknown artist")
        ),
        album: null,
        artwork:
          snippet.thumbnails?.high?.url ||
          snippet.thumbnails?.medium?.url ||
          snippet.thumbnails?.default?.url ||
          null,
        videoType: "YOUTUBE_FALLBACK",
        sourceQuality: 0
      };

      track.canonicalKey = makeCanonicalKey(track.artist, track.title);
      track.sourceQuality = scoreSourceQuality(track);
      return track;
    });

  return cleanAndRankSongResults(
    tracks,
    query,
    20
  );
}


function cleanFallbackChannelName(value) {
  return String(value || "")
    .replace(/\s*-\s*Topic\s*$/i, "")
    .replace(/VEVO$/i, "")
    .trim();
}


// =================================================================
// GENRE + DISCOVERY INTELLIGENCE
// =================================================================

const MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2/";
const LISTENBRAINZ_BASE = "https://api.listenbrainz.org/1/";
const MUSICBRAINZ_USER_AGENT =
  "Veeb/0.3 (https://veeb.connorjedd.workers.dev)";

let musicBrainzQueue = Promise.resolve();
let musicBrainzNextRequestAt = 0;


async function enrichTrackBrainzAndApplySignal(
  env,
  userId,
  trackId,
  delta,
  type
) {
  try {
    let track = await getStoredTrack(env, trackId);

    if (!track?.artist || !track?.title) {
      return;
    }

    let genres = parseStoredGenres(track.genresJson);
    const hadGenres = genres.length > 0;
    let recordingMbid = track.musicBrainzRecordingId || null;
    let artistMbid = track.musicBrainzArtistId || null;
    let matchScore = Number(track.brainzMatchScore || 0);
    let identityUpdated = false;

    const nowSeconds = Math.floor(Date.now() / 1000);
    const lastAttempt = Number(track.brainzLastAttemptAt || 0);
    const identityNeedsAttempt =
      (!recordingMbid || !artistMbid) &&
      (!lastAttempt || lastAttempt < nowSeconds - 60 * 60 * 24 * 7);
    const genreNeedsAttempt =
      !genres.length &&
      (!lastAttempt || lastAttempt < nowSeconds - 60 * 60 * 24 * 30);

    if (identityNeedsAttempt || genreNeedsAttempt) {
      await env.DB.prepare(`
        UPDATE tracks
        SET brainz_last_attempt_at = unixepoch()
        WHERE id = ?
      `)
        .bind(trackId)
        .run();
      const match = await resolveMusicBrainzRecording(
        track.artist,
        track.title,
        track.durationSeconds
      );

      if (match) {
        const oldRecordingMbid = recordingMbid;
        const oldArtistMbid = artistMbid;
        recordingMbid = match.recordingMbid || recordingMbid;
        artistMbid = match.artistMbid || artistMbid;
        identityUpdated =
          (!oldRecordingMbid && !!recordingMbid) ||
          (!oldArtistMbid && !!artistMbid);
        matchScore = Number(match.matchScore || matchScore || 0);

        const detail = recordingMbid
          ? await getBrainzRecordingMetadata(recordingMbid)
          : null;

        if (detail?.artistMbid) {
          artistMbid = detail.artistMbid;
        }

        const enrichedGenres = detail?.genres || match.genres || [];

        if (enrichedGenres.length) {
          genres = enrichedGenres;
        }

        await env.DB.prepare(`
          UPDATE tracks
          SET
            genres_json = COALESCE(?, genres_json),
            musicbrainz_recording_id = COALESCE(?, musicbrainz_recording_id),
            musicbrainz_artist_id = COALESCE(?, musicbrainz_artist_id),
            brainz_match_score = MAX(COALESCE(brainz_match_score, 0), ?),
            brainz_last_attempt_at = unixepoch(),
            last_enriched_at = unixepoch()
          WHERE id = ?
        `)
          .bind(
            genres.length ? JSON.stringify(genres) : null,
            recordingMbid,
            artistMbid,
            matchScore,
            trackId
          )
          .run();

        if (artistMbid) {
          await env.DB.prepare(`
            UPDATE artist_preferences
            SET musicbrainz_artist_id = COALESCE(musicbrainz_artist_id, ?)
            WHERE user_id = ?
              AND artist_key = ?
          `)
            .bind(
              artistMbid,
              userId,
              normalizeArtistKey(track.artist)
            )
            .run();
        }

        track = {
          ...track,
          musicBrainzRecordingId: recordingMbid,
          musicBrainzArtistId: artistMbid,
          genresJson: genres.length ? JSON.stringify(genres) : track.genresJson
        };
      } else {
        await env.DB.prepare(`
          UPDATE tracks
          SET brainz_last_attempt_at = unixepoch()
          WHERE id = ?
        `)
          .bind(trackId)
          .run();
      }
    }

    if (identityUpdated) {
      await invalidateRecommendationCache(env, userId);
    }

    if (!hadGenres && genres.length && delta) {
      await updateGenrePreferences(
        env,
        userId,
        genres,
        delta * 0.40,
        type
      );

      await invalidateRecommendationCache(env, userId);
    }
  } catch (error) {
    console.error("Brainz enrichment failed:", error);
  }
}


async function musicBrainzFetch(url) {
  const job = musicBrainzQueue.then(async () => {
    const waitMs = Math.max(0, musicBrainzNextRequestAt - Date.now());

    if (waitMs > 0) {
      await sleep(waitMs);
    }

    const response = await fetch(url, {
      headers: {
        "Accept": "application/json",
        "User-Agent": MUSICBRAINZ_USER_AGENT
      }
    });

    musicBrainzNextRequestAt = Date.now() + 1100;
    return response;
  });

  musicBrainzQueue = job.catch(() => null);
  return job;
}


function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}


function escapeMusicBrainzTerm(value) {
  return String(value || "")
    .replace(/\\/g, "\\\\")
    .replace(/([+\-!(){}\[\]^~*?:/])/g, "\\$1")
    .replace(/&&/g, "\\&&")
    .replace(/\|\|/g, "\\||")
    .replace(/"/g, '\\"')
    .trim();
}


async function resolveMusicBrainzRecording(artist, title, durationSeconds = 0) {
  const cleanArtist = cleanBrainzArtistName(artist);
  const cleanTitle = canonicalDisplayTitle(title);

  if (!cleanArtist || !cleanTitle) {
    return null;
  }

  const query =
    'recording:"' + escapeMusicBrainzTerm(cleanTitle) + '"' +
    ' AND artist:"' + escapeMusicBrainzTerm(cleanArtist) + '"';

  const url = new URL(MUSICBRAINZ_BASE + "recording/");
  url.searchParams.set("query", query);
  url.searchParams.set("fmt", "json");
  url.searchParams.set("limit", "8");

  const response = await musicBrainzFetch(url.toString());

  if (!response.ok) {
    return null;
  }

  const data = await response.json();
  const recordings = Array.isArray(data.recordings) ? data.recordings : [];

  let best = null;
  let bestScore = -Infinity;

  for (const item of recordings) {
    const itemArtist = getMusicBrainzArtistCreditName(item);
    const titleScore = textSimilarity(
      normalizeTitleKey(cleanTitle),
      normalizeTitleKey(item.title || "")
    );
    const artistScore = textSimilarity(
      normalizeArtistKey(cleanArtist),
      normalizeArtistKey(itemArtist)
    );
    const apiScore = Number(item.score || 0) / 100;

    let durationScore = 0;
    const itemDuration = Number(item.length || 0) / 1000;

    if (durationSeconds > 0 && itemDuration > 0) {
      const diff = Math.abs(durationSeconds - itemDuration);
      durationScore = Math.max(-0.35, 0.25 - diff / 90);
    }

    const score =
      apiScore * 0.30 +
      titleScore * 0.38 +
      artistScore * 0.37 +
      durationScore;

    if (score > bestScore) {
      const firstArtist = item["artist-credit"]?.[0]?.artist || null;

      bestScore = score;
      best = {
        recordingMbid: item.id || null,
        artistMbid: firstArtist?.id || null,
        artist: itemArtist || cleanArtist,
        title: item.title || cleanTitle,
        matchScore: Number(Math.max(0, Math.min(1, score)).toFixed(3)),
        genres: normalizeBrainzTags(item.genres || [], item.tags || [])
      };
    }
  }

  return best && best.matchScore >= 0.58 ? best : null;
}


async function getBrainzRecordingMetadata(recordingMbid) {
  if (!recordingMbid) {
    return null;
  }

  const url = new URL(MUSICBRAINZ_BASE + "recording/" + recordingMbid);
  url.searchParams.set("inc", "artist-credits+genres+tags");
  url.searchParams.set("fmt", "json");

  const response = await musicBrainzFetch(url.toString());

  if (!response.ok) {
    return null;
  }

  const data = await response.json();
  const firstArtist = data["artist-credit"]?.[0]?.artist || null;

  let genres = normalizeBrainzTags(
    data.genres || [],
    data.tags || []
  );

  if (genres.length < 3 && firstArtist?.id) {
    const artistMeta = await getBrainzArtistMetadata(firstArtist.id);
    genres = mergeWeightedGenres(genres, artistMeta?.genres || []);
  }

  return {
    recordingMbid: data.id || recordingMbid,
    artistMbid: firstArtist?.id || null,
    artist: getMusicBrainzArtistCreditName(data),
    title: data.title || null,
    genres
  };
}


async function getBrainzArtistMetadata(artistMbid) {
  const url = new URL(MUSICBRAINZ_BASE + "artist/" + artistMbid);
  url.searchParams.set("inc", "genres+tags");
  url.searchParams.set("fmt", "json");

  const response = await musicBrainzFetch(url.toString());

  if (!response.ok) {
    return null;
  }

  const data = await response.json();

  return {
    artistMbid: data.id || artistMbid,
    artist: data.name || null,
    genres: normalizeBrainzTags(data.genres || [], data.tags || [])
  };
}


function getMusicBrainzArtistCreditName(value) {
  const credits = Array.isArray(value?.["artist-credit"])
    ? value["artist-credit"]
    : [];

  return credits
    .map(item => {
      const name = item?.name || item?.artist?.name || "";
      return name + String(item?.joinphrase || "");
    })
    .join("")
    .trim();
}


function cleanBrainzArtistName(value) {
  return String(value || "")
    .replace(/\s*-\s*Topic\s*$/i, "")
    .replace(/VEVO$/i, "")
    .replace(/\s+/g, " ")
    .trim();
}


function normalizeBrainzTags(genres, tags) {
  const combined = [];

  for (const item of Array.isArray(genres) ? genres : []) {
    combined.push({
      name: item?.name || "",
      count: Number(item?.count || 0),
      formalGenre: true
    });
  }

  for (const item of Array.isArray(tags) ? tags : []) {
    combined.push({
      name: item?.name || item?.tag || "",
      count: Number(item?.count || 0),
      formalGenre: !!item?.["genre_mbid"] || !!item?.genre_mbid
    });
  }

  return normalizeGenreTags(combined);
}


function normalizeGenreTags(tags) {
  const output = [];
  const seen = new Map();

  const noise = /^(seen live|favorites?|favourites?|awesome|love|beautiful|cool|spotify|albums i own|songs i own|male vocalists?|female vocalists?|american|british|australian|canadian|00s|10s|20s|80s|90s|2000s|2010s|2020s|music|english)$/i;

  const aliases = {
    "rnb": "r&b",
    "r and b": "r&b",
    "rhythm and blues": "r&b",
    "hiphop": "hip-hop",
    "hip hop": "hip-hop",
    "alt country": "alternative country",
    "alt-country": "alternative country",
    "singer songwriter": "singer-songwriter",
    "singer/songwriter": "singer-songwriter",
    "neo soul": "neo-soul",
    "indiepop": "indie pop",
    "indierock": "indie rock",
    "electropop": "electro-pop",
    "trip-hop": "trip hop"
  };

  for (const raw of Array.isArray(tags) ? tags : []) {
    let name = String(raw?.name || raw?.tag || "")
      .toLowerCase()
      .trim();

    if (!name || noise.test(name) || /^\d{4}s?$/.test(name)) {
      continue;
    }

    name = aliases[name] || name;

    if (name.length > 48) {
      continue;
    }

    const count = Math.max(0, Number(raw?.count || 0));
    const formalBonus = raw?.formalGenre ? 0.35 : 0;
    const weight = Math.min(
      1.35,
      0.52 + Math.log10(count + 1) * 0.28 + formalBonus
    );

    const existing = seen.get(name);

    if (!existing || weight > existing.weight) {
      seen.set(name, {
        name,
        weight: Number(weight.toFixed(3))
      });
    }
  }

  output.push(...seen.values());
  return output
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 8);
}


function mergeWeightedGenres(first, second) {
  const map = new Map();

  for (const item of [...(first || []), ...(second || [])]) {
    const name = String(item?.name || item || "").toLowerCase().trim();
    if (!name) continue;
    const weight = Number(item?.weight || 1);
    if (!map.has(name) || Number(map.get(name).weight || 0) < weight) {
      map.set(name, { name, weight });
    }
  }

  return [...map.values()]
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 8);
}


function canonicalDisplayTitle(value) {
  return String(value || "")
    .replace(/\[(official audio|official video|music video|lyrics?|visuali[sz]er)\]/gi, " ")
    .replace(/\((official audio|official video|music video|lyrics?|visuali[sz]er)\)/gi, " ")
    .replace(/\((?:\d{4}\s*)?remaster(?:ed)?[^)]*\)/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}


function textSimilarity(a, b) {
  const left = String(a || "").trim();
  const right = String(b || "").trim();

  if (!left || !right) return 0;
  if (left === right) return 1;
  if (left.includes(right) || right.includes(left)) return 0.82;

  const leftTokens = new Set(left.split(/\s+/).filter(Boolean));
  const rightTokens = new Set(right.split(/\s+/).filter(Boolean));
  let overlap = 0;

  for (const token of leftTokens) {
    if (rightTokens.has(token)) overlap++;
  }

  const union = new Set([...leftTokens, ...rightTokens]).size || 1;
  return overlap / union;
}


async function listenBrainzFetch(path, params = {}) {
  const url = new URL(LISTENBRAINZ_BASE + path.replace(/^\/+/, ""));

  for (const [key, value] of Object.entries(params)) {
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== undefined && item !== null && item !== "") {
          url.searchParams.append(key, String(item));
        }
      }
      continue;
    }

    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }

  const response = await fetch(url.toString(), {
    headers: {
      "Accept": "application/json",
      "User-Agent": MUSICBRAINZ_USER_AGENT
    }
  });

  if (!response.ok) {
    return null;
  }

  return response.json();
}


function flattenRecordingMbidRows(value, output = []) {
  if (!value || typeof value !== "object") {
    return output;
  }

  if (typeof value.recording_mbid === "string") {
    output.push(value);
  }

  if (Array.isArray(value)) {
    for (const item of value) {
      flattenRecordingMbidRows(item, output);
    }
    return output;
  }

  for (const child of Object.values(value)) {
    flattenRecordingMbidRows(child, output);
  }

  return output;
}


async function listenBrainzArtistRadio(
  artistMbid,
  mode = "medium",
  maxSimilarArtists = 6,
  maxRecordingsPerArtist = 3,
  popBegin = 10,
  popEnd = 95
) {
  if (!artistMbid) return [];

  const data = await listenBrainzFetch(
    "lb-radio/artist/" + encodeURIComponent(artistMbid),
    {
      mode,
      max_similar_artists: maxSimilarArtists,
      max_recordings_per_artist: maxRecordingsPerArtist,
      pop_begin: popBegin,
      pop_end: popEnd
    }
  );

  return flattenRecordingMbidRows(data)
    .filter(item => item?.recording_mbid)
    .map((item, index) => ({
      recordingMbid: item.recording_mbid,
      artistMbid: item.similar_artist_mbid || null,
      artist: item.similar_artist_name || null,
      totalListenCount: Number(item.total_listen_count || 0),
      match: Math.max(0.18, 1 - index * 0.045),
      mode
    }));
}


async function listenBrainzTagRadio(
  tags,
  count = 12,
  popBegin = 5,
  popEnd = 90,
  operator = "OR"
) {
  const cleanTags = (Array.isArray(tags) ? tags : [tags])
    .map(item => String(item || "").toLowerCase().trim())
    .filter(Boolean)
    .slice(0, 4);

  if (!cleanTags.length) return [];

  const data = await listenBrainzFetch(
    "lb-radio/tags",
    {
      tag: cleanTags,
      operator: cleanTags.length > 1 ? operator : undefined,
      pop_begin: popBegin,
      pop_end: popEnd,
      count
    }
  );

  return flattenRecordingMbidRows(data)
    .filter(item => item?.recording_mbid)
    .map((item, index) => ({
      recordingMbid: item.recording_mbid,
      match: Math.max(0.18, 1 - index * 0.05),
      seedGenres: cleanTags
    }));
}


async function getListenBrainzRecordingMetadata(recordingMbids) {
  const ids = [...new Set((recordingMbids || []).filter(Boolean))].slice(0, 50);

  if (!ids.length) return {};

  const data = await listenBrainzFetch(
    "metadata/recording/",
    {
      recording_mbids: ids.join(","),
      inc: "artist tag release"
    }
  );

  return data && typeof data === "object" ? data : {};
}


function genresFromListenBrainzMetadata(meta) {
  if (!meta || typeof meta !== "object") return [];

  const tag = meta.tag || {};
  const combined = [
    ...(Array.isArray(tag.recording) ? tag.recording : []),
    ...(Array.isArray(tag.artist) ? tag.artist : []),
    ...(Array.isArray(tag.release_group) ? tag.release_group : [])
  ].map(item => ({
    name: item?.tag || "",
    count: Number(item?.count || 0),
    formalGenre: !!item?.genre_mbid
  }));

  return normalizeGenreTags(combined);
}


async function resolveRecordingMbids(recordingMbids) {
  const ids = [...new Set((recordingMbids || []).filter(Boolean))];
  const output = new Map();

  for (let offset = 0; offset < ids.length; offset += 18) {
    const batch = ids.slice(offset, offset + 18);
    const query = batch.map(id => "rid:" + id).join(" OR ");
    const url = new URL(MUSICBRAINZ_BASE + "recording/");
    url.searchParams.set("query", query);
    url.searchParams.set("fmt", "json");
    url.searchParams.set("limit", String(Math.min(100, batch.length * 2)));

    const response = await musicBrainzFetch(url.toString());
    if (!response.ok) continue;

    const data = await response.json();

    for (const item of Array.isArray(data.recordings) ? data.recordings : []) {
      if (!item?.id || !batch.includes(item.id)) continue;
      const firstArtist = item["artist-credit"]?.[0]?.artist || null;
      output.set(item.id, {
        recordingMbid: item.id,
        title: item.title || "",
        artist: getMusicBrainzArtistCreditName(item),
        artistMbid: firstArtist?.id || null
      });
    }
  }

  return output;
}


function deriveAdjacentGenres(metadataByMbid, seedGenres, limit = 5) {
  const excluded = new Set(
    (seedGenres || []).map(item => String(item || "").toLowerCase().trim())
  );
  const scores = new Map();

  for (const meta of Object.values(metadataByMbid || {})) {
    for (const genre of genresFromListenBrainzMetadata(meta)) {
      const name = String(genre.name || "").toLowerCase().trim();
      if (!name || excluded.has(name)) continue;
      scores.set(
        name,
        (scores.get(name) || 0) + Number(genre.weight || 1)
      );
    }
  }

  return [...scores.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([genre, score], index) => ({
      genre,
      match: Math.max(0.25, Math.min(1, score / 6 - index * 0.03))
    }));
}


async function buildBrainzDiscoverySpecs(env, profile) {
  const origins = new Map();
  const baseRows = [];

  const addOrigin = (recordingMbid, origin) => {
    if (!recordingMbid) return;
    if (!origins.has(recordingMbid)) origins.set(recordingMbid, []);
    origins.get(recordingMbid).push(origin);
  };

  const artistSeedTracks = profile.topTracks
    .filter(track => track.musicBrainzArtistId)
    .slice(0, 2);

  const artistCalls = artistSeedTracks.map((track, index) =>
    listenBrainzArtistRadio(
      track.musicBrainzArtistId,
      index === 0 ? "medium" : "hard",
      6,
      3,
      index === 0 ? 20 : 5,
      index === 0 ? 95 : 72
    )
  );

  const genreSeeds = profile.genres
    .filter(genre => Number(genre.score || 0) > 0)
    .slice(0, 3);

  const genreCalls = [];

  if (genreSeeds[0]) {
    genreCalls.push(
      listenBrainzTagRadio([genreSeeds[0].genre], 12, 15, 90)
    );
  }

  if (genreSeeds.length >= 2) {
    genreCalls.push(
      listenBrainzTagRadio(
        genreSeeds.slice(0, 2).map(item => item.genre),
        14,
        12,
        88,
        "OR"
      )
    );
  }

  const [artistResults, genreResults] = await Promise.all([
    Promise.allSettled(artistCalls),
    Promise.allSettled(genreCalls)
  ]);

  artistResults.forEach((result, seedIndex) => {
    if (result.status !== "fulfilled") return;

    for (const item of result.value.slice(0, 14)) {
      baseRows.push(item);
      addOrigin(item.recordingMbid, {
        source: seedIndex === 0 ? "similar-track" : "similar-artist",
        score: (seedIndex === 0 ? 5.0 : 3.2) + item.match * 3.4,
        sourceGenre: null
      });
    }
  });

  genreResults.forEach((result, index) => {
    if (result.status !== "fulfilled") return;

    const seedGenre = genreSeeds[index]?.genre || genreSeeds[0]?.genre || null;

    for (const item of result.value.slice(0, 14)) {
      baseRows.push(item);
      addOrigin(item.recordingMbid, {
        source: "genre",
        score: 2.7 + item.match * 2.4,
        sourceGenre: seedGenre
      });
    }
  });

  const baseIds = [...new Set(baseRows.map(item => item.recordingMbid).filter(Boolean))]
    .slice(0, 36);

  const baseLbMetadata = await getListenBrainzRecordingMetadata(baseIds);
  const adjacentGenres = deriveAdjacentGenres(
    baseLbMetadata,
    genreSeeds.map(item => item.genre),
    4
  );

  const adjacentCalls = adjacentGenres.slice(0, 2).map(item =>
    listenBrainzTagRadio([item.genre], 10, 5, 70)
  );

  const adjacentResults = await Promise.allSettled(adjacentCalls);
  const adjacentRows = [];

  adjacentResults.forEach((result, index) => {
    if (result.status !== "fulfilled") return;
    const adjacent = adjacentGenres[index];

    for (const item of result.value.slice(0, 9)) {
      adjacentRows.push(item);
      addOrigin(item.recordingMbid, {
        source: "adjacent-genre",
        score: 2.8 + Number(adjacent?.match || 0) * 2.8,
        sourceGenre: adjacent?.genre || null
      });
    }
  });

  const allIds = [...new Set([
    ...baseIds,
    ...adjacentRows.map(item => item.recordingMbid)
  ].filter(Boolean))].slice(0, 48);

  const extraIds = allIds.filter(id => !baseLbMetadata[id]);
  const extraLbMetadata = extraIds.length
    ? await getListenBrainzRecordingMetadata(extraIds)
    : {};

  const lbMetadata = {
    ...baseLbMetadata,
    ...extraLbMetadata
  };

  const resolved = await resolveRecordingMbids(allIds);
  const specs = [];

  for (const [recordingMbid, detail] of resolved.entries()) {
    if (!detail?.title || !detail?.artist) continue;

    const itemOrigins = origins.get(recordingMbid) || [];
    if (!itemOrigins.length) continue;

    const strongest = [...itemOrigins].sort((a, b) => b.score - a.score)[0];
    const genres = genresFromListenBrainzMetadata(lbMetadata[recordingMbid]);

    specs.push({
      query: detail.artist + " " + detail.title,
      source: strongest.source,
      sourceGenre: strongest.sourceGenre,
      sourceScore: strongest.score + Math.min(2.2, (itemOrigins.length - 1) * 0.6),
      expectedArtist: detail.artist,
      expectedTitle: detail.title,
      musicBrainzRecordingId: recordingMbid,
      musicBrainzArtistId: detail.artistMbid,
      genres
    });
  }

  return specs
    .sort((a, b) => b.sourceScore - a.sourceScore)
    .slice(0, 18);
}


async function invalidateRecommendationCache(env, userId) {
  await env.DB.prepare(`
    DELETE FROM recommendation_cache
    WHERE user_id = ?
  `)
    .bind(userId)
    .run();
}


async function getTasteProfile(env, userId) {
  const [topTracksResult, artistResult, genreResult, recentResult, allPrefsResult] =
    await Promise.all([
      env.DB.prepare(`
        SELECT
          t.id,
          t.title,
          t.artist,
          t.album,
          t.artwork,
          t.artist_id AS artistId,
          t.album_id AS albumId,
          t.duration_seconds AS durationSeconds,
          t.canonical_key AS canonicalKey,
          t.genres_json AS genresJson,
          t.video_type AS videoType,
          t.musicbrainz_recording_id AS musicBrainzRecordingId,
          t.musicbrainz_artist_id AS musicBrainzArtistId,
          t.brainz_match_score AS brainzMatchScore,
          p.score,
          p.play_count AS playCount,
          p.completion_count AS completionCount,
          p.skip_count AS skipCount,
          p.like_count AS likeCount,
          p.dislike_count AS dislikeCount,
          p.save_count AS saveCount,
          p.last_played_at AS lastPlayedAt
        FROM track_preferences p
        INNER JOIN tracks t ON t.id = p.track_id
        WHERE p.user_id = ?
          AND p.score > 0
          AND p.dislike_count = 0
        ORDER BY p.score DESC
        LIMIT 12
      `).bind(userId).all(),

      env.DB.prepare(`
        SELECT
          artist_key AS artistKey,
          artist_name AS artistName,
          artist_id AS artistId,
          musicbrainz_artist_id AS musicBrainzArtistId,
          score,
          play_count AS playCount,
          completion_count AS completionCount,
          skip_count AS skipCount,
          like_count AS likeCount,
          dislike_count AS dislikeCount,
          save_count AS saveCount,
          last_played_at AS lastPlayedAt
        FROM artist_preferences
        WHERE user_id = ?
        ORDER BY score DESC
        LIMIT 20
      `).bind(userId).all(),

      env.DB.prepare(`
        SELECT
          genre,
          score,
          play_count AS playCount,
          completion_count AS completionCount,
          skip_count AS skipCount,
          like_count AS likeCount,
          dislike_count AS dislikeCount,
          save_count AS saveCount,
          last_played_at AS lastPlayedAt
        FROM genre_preferences
        WHERE user_id = ?
        ORDER BY score DESC
        LIMIT 20
      `).bind(userId).all(),

      env.DB.prepare(`
        SELECT
          e.track_id AS trackId,
          e.event_type AS eventType,
          e.created_at AS createdAt,
          t.artist,
          t.genres_json AS genresJson
        FROM listening_events e
        LEFT JOIN tracks t ON t.id = e.track_id
        WHERE e.user_id = ?
          AND e.event_type IN ('play', 'complete', 'skip')
        ORDER BY e.created_at DESC
        LIMIT 60
      `).bind(userId).all(),

      env.DB.prepare(`
        SELECT
          track_id AS trackId,
          score,
          play_count AS playCount,
          completion_count AS completionCount,
          skip_count AS skipCount,
          like_count AS likeCount,
          dislike_count AS dislikeCount,
          save_count AS saveCount,
          last_played_at AS lastPlayedAt
        FROM track_preferences
        WHERE user_id = ?
        LIMIT 1000
      `).bind(userId).all()
    ]);

  let artists = artistResult.results || [];

  if (!artists.length) {
    const fallback = await env.DB.prepare(`
      SELECT
        lower(trim(t.artist)) AS artistKey,
        t.artist AS artistName,
        MAX(t.artist_id) AS artistId,
        MAX(t.musicbrainz_artist_id) AS musicBrainzArtistId,
        SUM(p.score) AS score,
        SUM(p.play_count) AS playCount,
        SUM(p.completion_count) AS completionCount,
        SUM(p.skip_count) AS skipCount,
        SUM(p.like_count) AS likeCount,
        SUM(p.dislike_count) AS dislikeCount,
        SUM(p.save_count) AS saveCount,
        MAX(p.last_played_at) AS lastPlayedAt
      FROM track_preferences p
      INNER JOIN tracks t ON t.id = p.track_id
      WHERE p.user_id = ?
        AND t.artist IS NOT NULL
      GROUP BY lower(trim(t.artist))
      ORDER BY score DESC
      LIMIT 20
    `)
      .bind(userId)
      .all();

    artists = fallback.results || [];
  }

  return {
    topTracks: topTracksResult.results || [],
    artists,
    genres: genreResult.results || [],
    recent: recentResult.results || [],
    allTrackPreferences: allPrefsResult.results || []
  };
}


async function buildRecommendations(
  env,
  userId,
  options = {}
) {
  const limit = Number(options.limit || 36);
  const seedTrackId = options.seedTrackId || null;
  const useCache = options.useCache !== false && !seedTrackId;

  if (useCache) {
    const cached = await env.DB.prepare(`
      SELECT payload, generated_at AS generatedAt
      FROM recommendation_cache
      WHERE user_id = ?
    `)
      .bind(userId)
      .first();

    if (
      cached?.payload &&
      Number(cached.generatedAt || 0) >
        Math.floor(Date.now() / 1000) - 1200
    ) {
      try {
        const parsed = JSON.parse(cached.payload);
        if (Array.isArray(parsed) && parsed.length) {
          return parsed.slice(0, limit);
        }
      } catch {}
    }
  }

  const profile = await getTasteProfile(env, userId);
  const candidates = new Map();

  const addCandidate = (track, source, sourceScore = 0, sourceGenre = null) => {
    if (!track?.id || !track?.title) return;

    const key =
      track.canonicalKey ||
      makeCanonicalKey(track.artist, track.title) ||
      track.id;

    const existing = candidates.get(key);

    if (!existing) {
      candidates.set(key, {
        ...track,
        canonicalKey: key,
        _sourceScore: sourceScore,
        _sources: [source],
        _sourceGenres: sourceGenre ? [sourceGenre] : []
      });
      return;
    }

    existing._sourceScore += sourceScore;

    if (!existing._sources.includes(source)) {
      existing._sources.push(source);
    }

    if (
      sourceGenre &&
      !existing._sourceGenres.includes(sourceGenre)
    ) {
      existing._sourceGenres.push(sourceGenre);
    }

    if (
      Number(track.sourceQuality || 0) >
      Number(existing.sourceQuality || 0)
    ) {
      const sourceMeta = {
        _sourceScore: existing._sourceScore,
        _sources: existing._sources,
        _sourceGenres: existing._sourceGenres
      };
      Object.assign(existing, track, sourceMeta);
    }
  };

  for (const track of profile.topTracks.slice(0, 12)) {
    addCandidate(
      storedRowToTrack(track),
      "known",
      Math.max(0, Math.min(8, Number(track.score || 0) * 0.35))
    );
  }

  const seeds = [];

  if (seedTrackId) {
    seeds.push(seedTrackId);
  }

  for (const track of profile.topTracks) {
    if (!seeds.includes(track.id)) {
      seeds.push(track.id);
    }
    if (seeds.length >= 3) break;
  }

  const seedPrimaryGenres = new Map();

  for (const seed of seeds) {
    let sourceTrack = profile.topTracks.find(track => track.id === seed) || null;

    if (!sourceTrack && seed === seedTrackId) {
      sourceTrack = await getStoredTrack(env, seed);
    }

    const seedGenres = parseStoredGenres(sourceTrack?.genresJson);
    if (seedGenres.length) {
      seedPrimaryGenres.set(
        seed,
        String(seedGenres[0].name || seedGenres[0]).toLowerCase()
      );
    }
  }

  const radioResults = await Promise.allSettled(
    seeds.map(seed => ytmGetRadio(seed, 35))
  );

  radioResults.forEach((result, seedIndex) => {
    if (result.status !== "fulfilled") return;

    result.value.forEach((track, index) => {
      addCandidate(
        track,
        "ytm-radio",
        Math.max(1.5, 6.5 - seedIndex * 1.2 - index * 0.035),
        seedPrimaryGenres.get(seeds[seedIndex]) || null
      );
    });
  });

  // Full Brainz discovery is only done for the cached home mix.
  // Per-track queue refreshes stay fast and use YT Music radio plus the
  // learned Veeb profile.
  if (!seedTrackId) {
    try {
      const discoverySpecs = await buildBrainzDiscoverySpecs(env, profile);

      const resolved = await mapLimit(
        discoverySpecs,
        4,
        async spec => {
          try {
            const results = await ytmSearchSongs(spec.query, 6);
            const best = chooseExternalMatch(results, spec);

            if (!best) {
              return null;
            }

            best.musicBrainzRecordingId =
              spec.musicBrainzRecordingId || null;
            best.musicBrainzArtistId =
              spec.musicBrainzArtistId || null;
            best.genres = spec.genres || [];

            return { track: best, spec };
          } catch {
            return null;
          }
        }
      );

      for (const item of resolved) {
        if (!item) continue;

        addCandidate(
          item.track,
          item.spec.source,
          item.spec.sourceScore,
          item.spec.sourceGenre || null
        );
      }
    } catch (error) {
      console.error("Brainz discovery failed:", error);
    }
  }

  let list = [...candidates.values()];

  if (!list.length) {
    return [];
  }

  const trackPrefMap = new Map(
    profile.allTrackPreferences.map(pref => [pref.trackId, pref])
  );

  const artistPrefMap = new Map(
    profile.artists.map(pref => [
      pref.artistKey || normalizeArtistKey(pref.artistName),
      pref
    ])
  );

  const genrePrefMap = new Map(
    profile.genres.map(pref => [pref.genre, pref])
  );

  const recentTrackTimes = new Map();
  const recentArtistCounts = new Map();
  const recentGenreCounts = new Map();

  for (const item of profile.recent) {
    if (!recentTrackTimes.has(item.trackId)) {
      recentTrackTimes.set(item.trackId, Number(item.createdAt || 0));
    }

    const artistKey = normalizeArtistKey(item.artist);

    if (artistKey) {
      recentArtistCounts.set(
        artistKey,
        (recentArtistCounts.get(artistKey) || 0) + 1
      );
    }

    for (const genre of parseStoredGenres(item.genresJson)) {
      const genreKey = String(genre.name || genre).toLowerCase().trim();
      if (!genreKey) continue;
      recentGenreCounts.set(
        genreKey,
        (recentGenreCounts.get(genreKey) || 0) + 1
      );
    }
  }

  list = list
    .map(track => {
      const pref = trackPrefMap.get(track.id);
      const artistKey = normalizeArtistKey(track.artist);
      const artistPref = artistPrefMap.get(artistKey);
      const storedGenres = Array.isArray(track.genres)
        ? track.genres
        : parseStoredGenres(track.genresJson);
      const candidateGenres = new Set([
        ...storedGenres.map(item => String(item.name || item).toLowerCase()),
        ...(track._sourceGenres || []).map(item => String(item).toLowerCase())
      ]);

      let score = Number(track._sourceScore || 0);

      if (pref) {
        if (Number(pref.dislikeCount || 0) > 0) {
          score -= 1000;
        }

        score += clampNumber(Number(pref.score || 0) * 0.9, -20, 18);
      } else {
        score += 3.2;
      }

      if (artistPref) {
        score += clampNumber(
          Number(artistPref.score || 0) * 0.34,
          -9,
          10
        );
      } else if (artistKey) {
        score += 1.25;
      }

      let genreAffinity = 0;

      for (const genre of candidateGenres) {
        const genrePref = genrePrefMap.get(genre);
        if (!genrePref) continue;
        genreAffinity += clampNumber(
          Number(genrePref.score || 0) * 0.22,
          -5,
          5
        );
      }

      score += clampNumber(genreAffinity, -8, 10);

      // Avoid turning a strong genre preference into a one-genre tunnel.
      let genreFatigue = 0;
      for (const genre of candidateGenres) {
        const count = recentGenreCounts.get(genre) || 0;
        if (count > 5) {
          genreFatigue += Math.min(5, (count - 5) * 0.65);
        }
      }
      score -= genreFatigue;

      // Small reward for an adjacent genre that has not been hammered recently.
      if (
        track._sources.includes("adjacent-genre") &&
        [...candidateGenres].every(genre => (recentGenreCounts.get(genre) || 0) < 3)
      ) {
        score += 1.75;
      }

      const lastPlayed =
        Number(pref?.lastPlayedAt || 0) ||
        Number(recentTrackTimes.get(track.id) || 0);

      if (lastPlayed) {
        const ageDays =
          (Date.now() / 1000 - lastPlayed) / 86400;

        if (ageDays < 0.75) score -= 15;
        else if (ageDays < 3) score -= 9;
        else if (ageDays < 7) score -= 5;
        else if (ageDays < 21) score -= 2;
        else if (ageDays > 60) score += 1.5;
      } else {
        score += 2.25;
      }

      const recentArtistCount =
        recentArtistCounts.get(artistKey) || 0;

      if (recentArtistCount > 2) {
        score -= (recentArtistCount - 2) * 2.25;
      }

      score -= Math.max(0, getVariantPenalty(track, "") * 0.4);
      score += Math.random() * 1.4;

      let bucket = "discovery";

      if (
        Number(pref?.score || 0) > 2.5 ||
        Number(artistPref?.score || 0) > 6
      ) {
        bucket = "comfort";
      } else if (
        track._sources.includes("ytm-radio") ||
        track._sources.includes("similar-track") ||
        track._sources.includes("similar-artist") ||
        track._sources.includes("adjacent-genre")
      ) {
        bucket = "edge";
      }

      if (track._sources.includes("genre") && !pref) {
        bucket = "discovery";
      }

      return {
        ...track,
        _score: score,
        _bucket: bucket,
        genres: storedGenres,
        veebReason: buildRecommendationReason(track, bucket)
      };
    })
    .filter(track => track._score > -100)
    .sort((a, b) => b._score - a._score);

  const selected = selectRecommendationMix(
    list,
    limit
  );

  const output = selected.map(track => ({
    id: track.id,
    title: track.title,
    artist: track.artist,
    artistId: track.artistId || null,
    album: track.album || null,
    albumId: track.albumId || null,
    artwork: track.artwork || null,
    durationSeconds: Number(track.durationSeconds || 0) || null,
    canonicalKey: track.canonicalKey,
    videoType: track.videoType || null,
    sourceQuality: Number(track.sourceQuality || 0),
    musicBrainzRecordingId: track.musicBrainzRecordingId || null,
    musicBrainzArtistId: track.musicBrainzArtistId || null,
    genres: track.genres || [],
    veebReason: track.veebReason,
    veebBucket: track._bucket
  }));

  if (useCache && output.length) {
    await env.DB.prepare(`
      INSERT INTO recommendation_cache (
        user_id,
        payload,
        generated_at
      )
      VALUES (?, ?, unixepoch())
      ON CONFLICT(user_id)
      DO UPDATE SET
        payload = excluded.payload,
        generated_at = unixepoch()
    `)
      .bind(userId, JSON.stringify(output))
      .run();
  }

  return output;
}


function storedRowToTrack(row) {
  return {
    id: row.id,
    title: row.title,
    artist: row.artist,
    artistId: row.artistId || null,
    album: row.album || null,
    albumId: row.albumId || null,
    artwork: row.artwork || null,
    durationSeconds: Number(row.durationSeconds || 0) || null,
    canonicalKey:
      row.canonicalKey ||
      makeCanonicalKey(row.artist, row.title),
    genresJson: row.genresJson || null,
    videoType: row.videoType || null,
    sourceQuality: Number(row.sourceQuality || 0),
    musicBrainzRecordingId: row.musicBrainzRecordingId || null,
    musicBrainzArtistId: row.musicBrainzArtistId || null,
    brainzMatchScore: Number(row.brainzMatchScore || 0)
  };
}


function chooseExternalMatch(results, spec) {
  if (!Array.isArray(results) || !results.length) {
    return null;
  }

  const expectedArtist = normalizeArtistKey(spec.expectedArtist || "");
  const expectedTitle = normalizeTitleKey(spec.expectedTitle || "");

  let best = null;
  let bestScore = -Infinity;

  for (const track of results) {
    let score = Number(track.sourceQuality || 0);
    const artist = normalizeArtistKey(track.artist);
    const title = normalizeTitleKey(track.title);

    if (expectedArtist) {
      if (artist === expectedArtist) score += 12;
      else if (artist.includes(expectedArtist) || expectedArtist.includes(artist)) score += 6;
      else score -= 5;
    }

    if (expectedTitle) {
      if (title === expectedTitle) score += 14;
      else if (title.includes(expectedTitle) || expectedTitle.includes(title)) score += 5;
      else score -= 4;
    }

    if (score > bestScore) {
      bestScore = score;
      best = track;
    }
  }

  return best;
}


function selectRecommendationMix(tracks, limit) {
  const target = {
    comfort: Math.round(limit * 0.55),
    edge: Math.round(limit * 0.30)
  };

  target.discovery = Math.max(
    0,
    limit - target.comfort - target.edge
  );

  const selected = [];
  const selectedIds = new Set();
  const selectedKeys = new Set();
  const artistCounts = new Map();

  const canAdd = track => {
    if (selectedIds.has(track.id)) return false;
    if (selectedKeys.has(track.canonicalKey)) return false;

    const artistKey = normalizeArtistKey(track.artist);
    const artistCount = artistCounts.get(artistKey) || 0;

    return !artistKey || artistCount < 2;
  };

  const add = track => {
    if (!canAdd(track)) return false;

    selected.push(track);
    selectedIds.add(track.id);
    selectedKeys.add(track.canonicalKey);

    const artistKey = normalizeArtistKey(track.artist);

    if (artistKey) {
      artistCounts.set(
        artistKey,
        (artistCounts.get(artistKey) || 0) + 1
      );
    }

    return true;
  };

  for (const bucket of ["comfort", "edge", "discovery"]) {
    let count = 0;

    for (const track of tracks) {
      if (track._bucket !== bucket) continue;
      if (count >= target[bucket]) break;
      if (add(track)) count++;
    }
  }

  for (const track of tracks) {
    if (selected.length >= limit) break;
    add(track);
  }

  return avoidAdjacentArtists(selected);
}


function avoidAdjacentArtists(tracks) {
  const output = [];
  const remaining = [...tracks];

  while (remaining.length) {
    const previousArtist = output.length
      ? normalizeArtistKey(output[output.length - 1].artist)
      : "";

    let index = remaining.findIndex(
      track => normalizeArtistKey(track.artist) !== previousArtist
    );

    if (index < 0) index = 0;

    output.push(
      remaining.splice(index, 1)[0]
    );
  }

  return output;
}


function buildRecommendationReason(track, bucket) {
  if (track._sources.includes("adjacent-genre") && track._sourceGenres?.length) {
    return "Genre neighbour: " + track._sourceGenres[0];
  }

  if (track._sources.includes("genre") && track._sourceGenres?.length) {
    return "Genre fit: " + track._sourceGenres[0];
  }

  if (track._sources.includes("similar-track")) {
    return "Adjacent to tracks you like";
  }

  if (track._sources.includes("similar-artist")) {
    return "Adjacent artist";
  }

  if (track._sources.includes("ytm-radio")) {
    return bucket === "comfort"
      ? "Strong fit"
      : "Related discovery";
  }

  if (bucket === "comfort") {
    return "Based on your history";
  }

  return "Discovery";
}


function clampNumber(value, min, max) {
  return Math.max(min, Math.min(max, value));
}


async function mapLimit(items, concurrency, worker) {
  const results = new Array(items.length);
  let cursor = 0;

  const runners = new Array(
    Math.min(concurrency, items.length)
  )
    .fill(null)
    .map(async () => {
      while (true) {
        const index = cursor++;
        if (index >= items.length) return;
        results[index] = await worker(items[index], index);
      }
    });

  await Promise.all(runners);
  return results;
}


// =================================================================
// PLAYBACK
// =================================================================

function getYouTubeResolverConfig(env) {
  const baseUrl = String(
    env.YOUTUBE_RESOLVER_URL || ""
  ).trim().replace(/\/+$/, "");

  const secret = String(
    env.YOUTUBE_RESOLVER_SECRET || ""
  ).trim();

  if (!baseUrl) {
    throw new Error(
      "Missing YOUTUBE_RESOLVER_URL Worker secret/variable"
    );
  }

  if (!/^https:\/\//i.test(baseUrl)) {
    throw new Error(
      "YOUTUBE_RESOLVER_URL must use HTTPS"
    );
  }

  if (!secret) {
    throw new Error(
      "Missing YOUTUBE_RESOLVER_SECRET Worker secret"
    );
  }

  return {
    baseUrl,
    secret
  };
}


async function getPlayableUrl(
  trackId,
  env
) {
  const resolver = getYouTubeResolverConfig(env);

  const response = await fetch(
    resolver.baseUrl
    + "/resolve/"
    + encodeURIComponent(trackId),
    {
      method: "GET",
      headers: {
        "Accept": "application/json",
        "Authorization": "Bearer " + resolver.secret
      },
      redirect: "follow"
    }
  );

  if (!response.ok) {
    const message = await response.text();

    console.error(
      "YouTube resolver metadata failed:",
      response.status,
      message.slice(0, 1000)
    );

    throw new Error(
      "YouTube resolver returned HTTP "
      + response.status
    );
  }

  const data = await response.json();

  return {
    provider: data.provider || "yt-dlp",
    title: data.title || null,
    duration: Number(data.duration || 0),
    formatId: data.formatId || null,
    ext: data.ext || null,
    audioCodec: data.audioCodec || null,
    abr: data.abr || null,
    proxied: true
  };
}


async function wakeYouTubeResolver(env) {
  const resolver = getYouTubeResolverConfig(env);

  const response = await fetch(
    resolver.baseUrl + "/health",
    {
      method: "GET",
      headers: {
        "Accept": "application/json",
        "Authorization": "Bearer " + resolver.secret
      },
      redirect: "follow"
    }
  );

  return response.ok;
}


async function prefetchPlayableAudio(
  trackId,
  env,
  intent
) {
  const resolver = getYouTubeResolverConfig(env);

  const response = await fetch(
    resolver.baseUrl
    + "/prefetch/"
    + encodeURIComponent(trackId)
    + (intent ? "?intent=1" : ""),
    {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Authorization": "Bearer " + resolver.secret
      },
      redirect: "follow"
    }
  );

  if (!response.ok && response.status !== 202) {
    const message = await response.text().catch(() => "");
    throw new Error(
      "Resolver prefetch returned HTTP "
      + response.status
      + (message ? ": " + message.slice(0, 300) : "")
    );
  }

  return true;
}


async function streamPlayableAudio(
  request,
  trackId,
  env,
  ctx
) {
  const resolver = getYouTubeResolverConfig(env);
  const cache = caches.default;

  const canonicalUrl = new URL(request.url);
  canonicalUrl.pathname =
    "/__veeb_audio_cache/"
    + encodeURIComponent(trackId);
  canonicalUrl.search = "";

  const canonicalKey = new Request(
    canonicalUrl.toString(),
    { method: "GET" }
  );

  // Cloudflare's Cache API can satisfy Range requests from a cached full
  // response. This keeps repeat plays and browser follow-up range reads away
  // from Render entirely.
  if (request.method === "GET") {
    const lookupHeaders = new Headers();
    const requestedRange = request.headers.get("Range");

    if (requestedRange) {
      lookupHeaders.set("Range", requestedRange);
    }

    const cacheLookup = new Request(
      canonicalUrl.toString(),
      {
        method: "GET",
        headers: lookupHeaders
      }
    );

    const cached = await cache.match(cacheLookup);

    if (cached) {
      const cachedHeaders = new Headers(cached.headers);
      cachedHeaders.set("X-Veeb-Edge-Cache", "HIT");
      cachedHeaders.set("X-Veeb-Playback-Provider", "cloudflare-edge-cache");

      return new Response(
        cached.body,
        {
          status: cached.status,
          statusText: cached.statusText,
          headers: cachedHeaders
        }
      );
    }
  }

  const upstreamHeaders = new Headers();
  upstreamHeaders.set("Accept", "*/*");
  upstreamHeaders.set(
    "Authorization",
    "Bearer " + resolver.secret
  );

  // On an edge-cache miss request the completed file from Render, not a
  // partial range. A full 200 response is cacheable; Cloudflare then handles
  // subsequent Range slicing itself.
  const upstream = await fetch(
    resolver.baseUrl
    + "/stream/"
    + encodeURIComponent(trackId),
    {
      method:
        request.method === "HEAD"
          ? "HEAD"
          : "GET",
      headers: upstreamHeaders,
      redirect: "follow"
    }
  );

  if (!upstream.ok && upstream.status !== 206) {
    let upstreamMessage = "";

    try {
      upstreamMessage = await upstream.text();
    } catch (_) {}

    console.error(
      "YouTube resolver stream failed:",
      upstream.status,
      upstream.statusText,
      upstreamMessage.slice(0, 1000)
    );

    throw new Error(
      "Resolver audio stream returned HTTP "
      + upstream.status
    );
  }

  const headers = new Headers(upstream.headers);

  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "audio/mp4");
  }

  if (!headers.has("Accept-Ranges")) {
    headers.set("Accept-Ranges", "bytes");
  }

  headers.set("X-Veeb-Playback-Provider", "youtube-resolver");
  headers.set("X-Veeb-Edge-Cache", "MISS");

  if (request.method === "HEAD") {
    headers.set("Cache-Control", "no-store");
    return new Response(null, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers
    });
  }

  // Store the completed audio at the Cloudflare edge for a day. Render's free
  // filesystem disappears on spin-down, while this gives Veeb another cache
  // layer that can survive a resolver restart for the user's usual edge POP.
  headers.set(
    "Cache-Control",
    "public, max-age=3600, s-maxage=86400"
  );

  const response = new Response(
    upstream.body,
    {
      status: upstream.status,
      statusText: upstream.statusText,
      headers
    }
  );

  const resolverCacheState = upstream.headers.get("X-Veeb-Cache") || "";

  if (
    upstream.status === 200 &&
    resolverCacheState === "HIT" &&
    ctx?.waitUntil
  ) {
    const cacheHeaders = new Headers(headers);
    cacheHeaders.delete("X-Veeb-Edge-Cache");
    cacheHeaders.set("X-Veeb-Edge-Cache", "STORED");

    const cacheResponse = new Response(
      response.clone().body,
      {
        status: 200,
        headers: cacheHeaders
      }
    );

    ctx.waitUntil(
      cache.put(
        canonicalKey,
        cacheResponse
      ).catch(error => {
        console.error(
          "Veeb edge audio cache put failed:",
          trackId,
          error
        );
      })
    );
  }

  return response;
}


function compareAudioFormats(
  a,
  b
) {
  if (
    a.itag === 251
    &&
    b.itag !== 251
  ) {
    return -1;
  }

  if (
    b.itag === 251
    &&
    a.itag !== 251
  ) {
    return 1;
  }

  if (
    a.itag === 140
    &&
    b.itag !== 140
  ) {
    return -1;
  }

  if (
    b.itag === 140
    &&
    a.itag !== 140
  ) {
    return 1;
  }

  return (
    Number(
      b.bitrate || 0
    )
    -
    Number(
      a.bitrate || 0
    )
  );
}


function getGoogleVideoExpiry(
  streamUrl
) {
  try {
    const parsed =
      new URL(
        streamUrl
      );

    const expire =
      parsed.searchParams.get(
        "expire"
      );

    return expire
      ? Number(expire)
      : null;

  } catch {
    return null;
  }
}


function decodeYouTubeText(
  value
) {
  return String(
    value || ""
  )
    .replaceAll(
      "&amp;",
      "&"
    )
    .replaceAll(
      "&quot;",
      '"'
    )
    .replaceAll(
      "&#39;",
      "'"
    )
    .replaceAll(
      "&lt;",
      "<"
    )
    .replaceAll(
      "&gt;",
      ">"
    );
}

// =================================================================
// HELPERS
// =================================================================

function normalizeEmail(value) {
  return String(value || "")
    .trim()
    .toLowerCase();
}


function isValidEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(
    value
  );
}


function getCookie(request, name) {
  const cookieHeader =
    request.headers.get("Cookie");

  if (!cookieHeader) {
    return null;
  }

  const cookies =
    cookieHeader.split(";");

  for (const cookie of cookies) {
    const [key, ...rest] =
      cookie.trim().split("=");

    if (key === name) {
      return rest.join("=");
    }
  }

  return null;
}


async function sha256Hex(value) {
  const bytes =
    new TextEncoder().encode(value);

  const hash =
    await crypto.subtle.digest(
      "SHA-256",
      bytes
    );

  return [...new Uint8Array(hash)]
    .map(
      byte =>
        byte
          .toString(16)
          .padStart(2, "0")
    )
    .join("");
}


function bytesToBase64(bytes) {
  let binary = "";

  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }

  return btoa(binary);
}


function base64ToBytes(value) {
  const binary = atob(value);

  const bytes =
    new Uint8Array(binary.length);

  for (
    let i = 0;
    i < binary.length;
    i++
  ) {
    bytes[i] =
      binary.charCodeAt(i);
  }

  return bytes;
}


function bytesToBase64Url(bytes) {
  return bytesToBase64(bytes)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}


function apiFailure(context, error) {
  const message =
    error && typeof error.message === "string"
      ? error.message
      : String(error);

  console.error(`Veeb API error (${context}):`, error);

  return json(
    {
      error: `Veeb ${context} failed: ${message}`
    },
    500
  );
}


function json(data, status = 200) {
  return Response.json(
    data,
    {
      status,
      headers: {
        "Cache-Control": "no-store"
      }
    }
  );
}


function jsonWithCookie(
  data,
  cookie,
  status = 200
) {
  const headers =
    new Headers();

  headers.set(
    "Content-Type",
    "application/json; charset=UTF-8"
  );

  headers.set(
    "Cache-Control",
    "no-store"
  );

  headers.append(
    "Set-Cookie",
    cookie
  );

  return new Response(
    JSON.stringify(data),
    {
      status,
      headers
    }
  );
}


// =================================================================
// SINGLE PAGE APP
// =================================================================

const APP_HTML = `
<!DOCTYPE html>
<html lang="en">

<head>

<meta charset="UTF-8">

<meta
  name="viewport"
  content="width=device-width,initial-scale=1,viewport-fit=cover"
>

<title>Veeb</title>

<link
  rel="stylesheet"
  href="https://cdn.jsdelivr.net/npm/@phosphor-icons/web@2.1.2/src/regular/style.css"
>
<link
  rel="stylesheet"
  href="https://cdn.jsdelivr.net/npm/@phosphor-icons/web@2.1.2/src/fill/style.css"
>

<style>

@font-face {
  font-family: "Veeb PolySans";
  src: url("https://cdn.shopify.com/s/files/1/0439/5597/8399/files/polysanstrial-median.woff2?v=1786620069") format("woff2");
  font-style: normal;
  font-weight: 500;
  font-display: swap;
}

@font-face {
  font-family: "Veeb Trade Gothic";
  src: url("https://cdn.shopify.com/s/files/1/0439/5597/8399/files/Trade-Gothic-Std-Extended.woff2?v=1786619972") format("woff2");
  font-style: normal;
  font-weight: 400;
  font-display: swap;
}

@font-face {
  font-family: "Veeb Trade Gothic";
  src: url("https://cdn.shopify.com/s/files/1/0439/5597/8399/files/Trade-Gothic-Std-Bold-Extended.woff2?v=1786619915") format("woff2");
  font-style: normal;
  font-weight: 700 900;
  font-display: swap;
}

:root {
  --bg: #101210;
  --surface: #181b19;
  --surface-2: #2a2a2a;

  --green: #20e47c;
  --green-soft: #9df3ad;

  --white: #edebeb;
  --yellow: #e8b73d;

  --muted: #858b87;
  --border: #303532;

  --danger: #ff685c;
}


* {
  box-sizing: border-box;
}


html,
body {
  margin: 0;

  min-height: 100%;

  background: var(--bg);

  color: var(--white);

  font-family:
    "Veeb PolySans",
    Arial,
    Helvetica,
    sans-serif;

  font-weight: 500;
}


button,
input {
  font: inherit;
}


button {
  border-radius: 0;
  box-shadow: none;
}


.hidden {
  display: none !important;
}


/* ================================================================
   TYPOGRAPHY
   PolySans carries readable/interface copy.
   Trade Gothic Extended carries identity, hierarchy and utility UI.
   ================================================================ */

.auth-logo,
.auth-big,
.auth-title,
.logo,
.hero h1 {
  font-family:
    "Veeb Trade Gothic",
    "Arial Narrow",
    Arial,
    sans-serif;

  font-weight: 900;
}

.auth-eyebrow,
.auth-tab,
.field label,
.auth-submit,
.small-button,
.kicker,
.search button,
.section-label,
.track-num,
.progress {
  font-family:
    "Veeb Trade Gothic",
    "Arial Narrow",
    Arial,
    sans-serif;
}

.auth-eyebrow,
.auth-tab,
.field label,
.auth-submit,
.small-button,
.kicker,
.search button,
.section-label {
  font-weight: 700;
}

.auth-foot,
.field input,
.user-email,
.search input,
.empty,
.track-title,
.track-sub,
.now-title,
.now-artist {
  font-family:
    "Veeb PolySans",
    Arial,
    Helvetica,
    sans-serif;

  font-weight: 500;
}


/* ================================================================
   AUTH
   ================================================================ */

#authScreen {
  min-height: 100vh;

  display: grid;

  grid-template-columns:
    1.15fr .85fr;
}


.auth-brand {
  position: relative;

  min-height: 100vh;

  display: flex;

  flex-direction: column;

  justify-content: space-between;

  padding:
    48px;

  background:
    var(--green);

  color:
    #07110b;
}


.auth-logo {
  font-size:
    30px;

  font-weight:
    900;

  letter-spacing:
    -0.8px;
}


.auth-big {
  max-width:
    700px;

  margin:
    auto 0;

  font-size:
    clamp(
      58px,
      8vw,
      118px
    );

  font-weight:
    900;

  line-height:
    .85;

  letter-spacing:
    -2.5px;
}


.auth-foot {
  max-width:
    420px;

  font-size:
    13px;

  line-height:
    1.5;

  font-weight:
    700;
}


.auth-panel {
  min-height:
    100vh;

  display:
    flex;

  align-items:
    center;

  justify-content:
    center;

  padding:
    48px;
}


.auth-box {
  width:
    min(440px, 100%);
}


.auth-eyebrow {
  margin-bottom:
    16px;

  color:
    var(--green);

  font-size:
    11px;

  font-weight:
    900;

  letter-spacing:
    3px;
}


.auth-title {
  margin:
    0 0 38px;

  font-size:
    48px;

  line-height:
    .95;

  letter-spacing:
    -3px;
}


.auth-tabs {
  display:
    grid;

  grid-template-columns:
    1fr 1fr;

  margin-bottom:
    32px;

  border-bottom:
    1px solid var(--border);
}


.auth-tab {
  border:
    0;

  border-bottom:
    2px solid transparent;

  padding:
    14px 4px;

  background:
    transparent;

  color:
    var(--muted);

  text-align:
    left;

  font-size:
    11px;

  font-weight:
    900;

  letter-spacing:
    2px;

  cursor:
    pointer;
}


.auth-tab.active {
  border-bottom-color:
    var(--green);

  color:
    var(--green);
}


.field {
  margin-bottom:
    22px;
}


.field label {
  display:
    block;

  margin-bottom:
    8px;

  color:
    var(--muted);

  font-size:
    10px;

  font-weight:
    900;

  letter-spacing:
    2px;
}


.field input {
  width:
    100%;

  padding:
    15px 0;

  border:
    0;

  border-bottom:
    1px solid var(--border);

  outline:
    0;

  background:
    transparent;

  color:
    var(--white);

  font-size:
    17px;
}


.field input:focus {
  border-bottom-color:
    var(--green);
}


.auth-submit {
  width:
    100%;

  margin-top:
    18px;

  border:
    0;

  padding:
    16px 20px;

  background:
    var(--green);

  color:
    #07110b;

  font-weight:
    900;

  letter-spacing:
    1px;

  cursor:
    pointer;
}


.auth-error {
  min-height:
    22px;

  margin-top:
    15px;

  color:
    var(--danger);

  font-size:
    12px;
}


/* ================================================================
   APP
   ================================================================ */

#appScreen {
  min-height:
    100vh;

  padding-bottom:
    126px;
}


.topbar {
  height:
    78px;

  display:
    flex;

  align-items:
    center;

  justify-content:
    space-between;

  padding:
    0 34px;

  position:
    sticky;

  top:
    0;

  z-index:
    50;

  border-bottom:
    1px solid var(--border);

  background:
    rgba(16,18,16,.97);
}


.logo {
  font-size:
    25px;

  font-weight:
    900;

  letter-spacing:
    -0.6px;
}


.logo-dot {
  display:
    inline-block;

  width:
    9px;

  height:
    9px;

  margin-left:
    7px;

  background:
    var(--green);
}


.top-actions {
  display:
    flex;

  align-items:
    center;

  gap:
    10px;
}


.user-email {
  margin-right:
    8px;

  color:
    var(--muted);

  font-size:
    11px;
}


.small-button {
  padding:
    9px 13px;

  border:
    1px solid var(--border);

  background:
    transparent;

  color:
    var(--white);

  font-size:
    10px;

  font-weight:
    900;

  letter-spacing:
    1px;

  cursor:
    pointer;
}


.small-button:hover {
  border-color:
    var(--green);

  color:
    var(--green);
}


.shell {
  width:
    min(
      1300px,
      calc(100% - 58px)
    );

  margin:
    0 auto;
}


.hero {
  padding:
    74px 0 54px;
}


.kicker {
  margin-bottom:
    16px;

  color:
    var(--green);

  font-size:
    11px;

  font-weight:
    900;

  letter-spacing:
    3px;
}


.hero h1 {
  margin:
    0;

  max-width:
    1000px;

  font-size:
    clamp(
      54px,
      8vw,
      116px
    );

  line-height:
    .86;

  font-weight:
    900;

  letter-spacing:
    -2.5px;
}


.search {
  max-width:
    850px;

  display:
    grid;

  grid-template-columns:
    1fr auto;

  margin-top:
    50px;

  border-bottom:
    2px solid var(--white);
}


.search input {
  padding:
    18px 0;

  border:
    0;

  outline:
    0;

  background:
    transparent;

  color:
    var(--white);

  font-size:
    18px;
}


.search button {
  border:
    0;

  padding:
    0 28px;

  background:
    var(--green);

  color:
    #07110b;

  font-weight:
    900;

  cursor:
    pointer;
}


.section-label {
  margin:
    0 0 18px;

  color:
    var(--green);

  font-size:
    11px;

  font-weight:
    900;

  letter-spacing:
    3px;
}


.empty {
  padding:
    30px 0;

  color:
    var(--muted);
}


.track-row {
  width:
    100%;

  display:
    grid;

  grid-template-columns:
    52px 62px 1fr auto;

  align-items:
    center;

  gap:
    16px;

  padding:
    13px 0;

  border:
    0;

  border-bottom:
    1px solid var(--border);

  background:
    transparent;

  color:
    var(--white);

  text-align:
    left;

  cursor:
    pointer;
}


.track-row:hover {
  background:
    var(--surface);
}


.track-row img {
  width:
    62px;

  height:
    62px;

  object-fit:
    cover;

  background:
    var(--surface-2);
}


.track-num {
  color:
    var(--muted);

  font-size:
    11px;
}


.track-title {
  font-size:
    15px;

  font-weight:
    800;
}


.track-sub {
  margin-top:
    5px;

  color:
    var(--muted);

  font-size:
    12px;
}


.track-play {
  padding:
    18px;

  color:
    var(--green);
}


.player {
  position:
    fixed;

  left:
    0;

  right:
    0;

  bottom:
    0;

  min-height:
    108px;

  overflow:
    hidden;

  border-top:
    1px solid var(--border);

  background:
    #151715;

  z-index:
    100;

  touch-action:
    none;

  user-select:
    none;
}


.player-swipe-surface {
  position:
    relative;

  z-index:
    2;

  width:
    100%;

  min-height:
    108px;

  display:
    grid;

  grid-template-columns:
    minmax(240px,1fr)
    minmax(400px,1.4fr)
    minmax(220px,1fr);

  align-items:
    center;

  gap:
    26px;

  padding:
    14px 28px;

  background:
    #151715;

  transform:
    translate3d(0,0,0);

  will-change:
    transform;
}


.player-swipe-preview {
  position:
    absolute;

  inset:
    0;

  z-index:
    1;

  pointer-events:
    none;

  will-change:
    transform, opacity;
}


.player.gesture-active .player-swipe-surface {
  cursor:
    grabbing;
}


.player-up-hint {
  position:
    absolute;

  top:
    3px;

  left:
    50%;

  z-index:
    5;

  display:
    flex;

  align-items:
    center;

  gap:
    4px;

  transform:
    translateX(-50%);

  color:
    var(--muted);

  font-size:
    8px;

  letter-spacing:
    .8px;

  pointer-events:
    none;

  opacity:
    .7;
}


.player-up-hint i {
  font-size:
    10px;
}


.now {
  display:
    flex;

  align-items:
    center;

  gap:
    14px;

  min-width:
    0;
}


.now img {
  width:
    72px;

  height:
    72px;

  object-fit:
    cover;

  background:
    var(--surface-2);
}


.now-title {
  overflow:
    hidden;

  white-space:
    nowrap;

  text-overflow:
    ellipsis;

  font-size:
    14px;

  font-weight:
    800;
}


.now-artist {
  margin-top:
    5px;

  color:
    var(--muted);

  font-size:
    12px;
}


.player-center {
  display:
    flex;

  flex-direction:
    column;

  gap:
    10px;
}


.controls {
  display:
    flex;

  align-items:
    center;

  justify-content:
    center;

  gap:
    18px;
}


.control {
  width:
    40px;

  height:
    40px;

  border:
    0;

  background:
    transparent;

  color:
    var(--white);

  cursor:
    pointer;
}


.control:hover {
  color:
    var(--green);
}


.play {
  width:
    50px;

  height:
    50px;

  border:
    0;

  background:
    var(--green);

  color:
    #07110b;

  font-size:
    18px;

  font-weight:
    900;

  cursor:
    pointer;
}


.progress {
  display:
    grid;

  grid-template-columns:
    40px 1fr 40px;

  align-items:
    center;

  gap:
    10px;

  color:
    var(--muted);

  font-size:
    10px;
}


.progress input {
  width:
    100%;

  accent-color:
    var(--green);
}


.player-actions {
  display:
    flex;

  justify-content:
    flex-end;

  gap:
    10px;
}


.toast {
  position:
    fixed;

  right:
    20px;

  bottom:
    130px;

  display:
    none;

  padding:
    12px 16px;

  background:
    var(--green);

  color:
    #07110b;

  font-size:
    11px;

  font-weight:
    900;

  z-index:
    500;
}


.veeb-spin {
  display: inline-block;
  animation: veeb-spin .8s linear infinite;
}

@keyframes veeb-spin {
  to {
    transform: rotate(360deg);
  }
}



/* ================================================================
   VEEB HOME / LIBRARY
   ================================================================ */

.nav-button.active,
.small-button.active {
  border-color: var(--green);
  color: var(--green);
}

.home-view {
  display: grid;
  gap: 48px;
  padding-bottom: 32px;
}

.home-section {
  min-width: 0;
}

.home-section-head {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 18px;
}

.home-section-head h2 {
  margin: 0;
  font-size: clamp(25px, 3vw, 42px);
  line-height: .95;
  letter-spacing: -1px;
}

.home-section-head p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}

.home-track-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 14px;
}

.home-track-card {
  appearance: none;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--white);
  padding: 0;
  text-align: left;
  cursor: pointer;
  min-width: 0;
}

.home-track-card:hover,
.taste-card:hover,
.playlist-card:hover {
  border-color: var(--green);
}

.home-track-card img {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  background: var(--surface-2);
}

.home-track-card-copy {
  padding: 13px;
}

.home-track-card-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
  font-weight: 800;
}

.home-track-card-sub {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 6px;
  color: var(--muted);
  font-size: 11px;
}

.taste-grid,
.playlist-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.taste-card,
.playlist-card {
  min-height: 116px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 18px;
  padding: 18px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--white);
  text-align: left;
  cursor: pointer;
}

.taste-card i,
.playlist-card i {
  color: var(--green);
  font-size: 24px;
}

.taste-card strong,
.playlist-card strong {
  display: block;
  font-size: 16px;
}

.taste-card span,
.playlist-card span {
  display: block;
  margin-top: 5px;
  color: var(--muted);
  font-size: 11px;
}

.library-empty {
  padding: 22px;
  border: 1px solid var(--border);
  color: var(--muted);
}

.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 800;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(0,0,0,.72);
}

.modal-card {
  width: min(520px, 100%);
  max-height: min(680px, 84vh);
  overflow: auto;
  border: 1px solid var(--border);
  background: #101210;
  padding: 24px;
}

.modal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 22px;
}

.modal-head h3 {
  margin: 0;
  font-size: 28px;
}

.modal-close {
  border: 0;
  background: transparent;
  color: var(--white);
  font-size: 24px;
  cursor: pointer;
}

.playlist-choice {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 0;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--white);
  text-align: left;
  cursor: pointer;
}

.playlist-choice i {
  color: var(--green);
}

.modal-create {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 10px;
  margin-top: 22px;
}

.modal-create input {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--white);
  outline: none;
}

.modal-create button {
  border: 0;
  padding: 0 16px;
  background: var(--green);
  color: #07110b;
  font-weight: 900;
  cursor: pointer;
}

.control.is-favourite {
  color: var(--green);
}

#audioEngine {
  display: none;
}


body.expanded-player-open {
  overflow:
    hidden;
}


.expanded-player {
  position:
    fixed;

  inset:
    0;

  z-index:
    900;

  display:
    flex;

  flex-direction:
    column;

  background:
    #101210;

  color:
    var(--white);

  transform:
    translate3d(0,100%,0);

  visibility:
    hidden;

  transition:
    transform 360ms cubic-bezier(.22,1,.36,1),
    visibility 0s linear 360ms;

  will-change:
    transform;

  touch-action:
    none;
}


.expanded-player.open {
  transform:
    translate3d(0,0,0);

  visibility:
    visible;

  transition:
    transform 360ms cubic-bezier(.22,1,.36,1),
    visibility 0s;
}


.expanded-player.dragging {
  transition:
    none;
}


.expanded-player-shell {
  width:
    min(520px, calc(100% - 36px));

  min-height:
    100%;

  margin:
    0 auto;

  display:
    flex;

  flex-direction:
    column;

  padding:
    max(12px, env(safe-area-inset-top))
    0
    calc(22px + env(safe-area-inset-bottom));
}


.expanded-handle-row {
  min-height:
    44px;

  display:
    flex;

  align-items:
    center;

  justify-content:
    space-between;
}


.expanded-handle {
  width:
    46px;

  height:
    4px;

  margin:
    0 auto;

  background:
    var(--muted);

  opacity:
    .6;
}


.expanded-close {
  width:
    44px;

  height:
    44px;

  border:
    0;

  background:
    transparent;

  color:
    var(--white);

  font-size:
    24px;

  cursor:
    pointer;
}


.expanded-spacer {
  width:
    44px;
}


.expanded-content {
  flex:
    1;

  min-height:
    0;

  display:
    flex;

  flex-direction:
    column;

  justify-content:
    center;

  gap:
    24px;
}


.expanded-art {
  display:
    block;

  width:
    min(78vw, 420px, 46vh);

  aspect-ratio:
    1;

  align-self:
    center;

  object-fit:
    cover;

  background:
    var(--surface-2);
}


.expanded-meta-row {
  display:
    grid;

  grid-template-columns:
    1fr auto;

  align-items:
    center;

  gap:
    16px;
}


.expanded-title {
  overflow:
    hidden;

  white-space:
    nowrap;

  text-overflow:
    ellipsis;

  font-family:
    "Veeb PolySans", sans-serif;

  font-size:
    clamp(24px, 6vw, 34px);

  line-height:
    1.02;
}


.expanded-artist,
.expanded-album {
  overflow:
    hidden;

  white-space:
    nowrap;

  text-overflow:
    ellipsis;

  margin-top:
    7px;

  color:
    var(--muted);

  font-family:
    "Veeb PolySans", sans-serif;

  font-size:
    14px;
}


.expanded-album {
  margin-top:
    3px;

  font-size:
    11px;
}


.expanded-like {
  width:
    48px;

  height:
    48px;

  border:
    0;

  background:
    transparent;

  color:
    var(--white);

  font-size:
    26px;

  cursor:
    pointer;
}


.expanded-like.is-favourite {
  color:
    var(--green);
}


.expanded-progress {
  display:
    grid;

  grid-template-columns:
    1fr;

  gap:
    8px;
}


.expanded-progress input {
  width:
    100%;

  accent-color:
    var(--green);
}


.expanded-times {
  display:
    flex;

  align-items:
    center;

  justify-content:
    space-between;

  color:
    var(--muted);

  font-size:
    10px;
}


.expanded-controls {
  display:
    grid;

  grid-template-columns:
    repeat(5, 1fr);

  align-items:
    center;

  gap:
    8px;
}


.expanded-control,
.expanded-play {
  height:
    54px;

  border:
    0;

  background:
    transparent;

  color:
    var(--white);

  font-size:
    23px;

  cursor:
    pointer;
}


.expanded-play {
  width:
    60px;

  height:
    60px;

  justify-self:
    center;

  background:
    var(--green);

  color:
    #07110b;

  font-size:
    22px;
}


.expanded-bottom-actions {
  display:
    grid;

  grid-template-columns:
    1fr 1fr;

  gap:
    10px;
}


.expanded-bottom-actions button {
  min-height:
    44px;
}


.now {
  cursor:
    pointer;
}


@media (prefers-reduced-motion: reduce) {
  .expanded-player,
  .player-swipe-surface,
  .player-swipe-preview {
    transition-duration:
      1ms !important;
  }
}

@media(max-width:1050px) {
  .home-track-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .taste-grid,
  .playlist-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media(max-width:800px) {

  #authScreen {
    grid-template-columns:
      1fr;
  }


  .auth-brand {
    min-height:
      280px;

    padding:
      28px;
  }


  .auth-big {
    font-size:
      62px;

    letter-spacing:
      -5px;
  }


  .auth-panel {
    min-height:
      auto;

    padding:
      42px 28px;
  }


  .topbar {
    padding:
      0 16px;
  }


  .user-email {
    display:
      none;
  }


  .shell {
    width:
      calc(100% - 32px);
  }


  .hero {
    padding:
      46px 0 40px;
  }


  .hero h1 {
    letter-spacing:
      -4px;
  }


  .player-swipe-surface {
    grid-template-columns:
      1fr auto;

    padding:
      12px 14px 10px;
  }


  .player-center {
    grid-column:
      1 / -1;
  }


  .home-track-grid {
    grid-template-columns:
      repeat(2, minmax(0, 1fr));
  }


  .taste-grid,
  .playlist-grid {
    grid-template-columns:
      1fr 1fr;
  }


  .top-actions {
    gap:
      6px;
  }


  .top-actions .small-button {
    padding:
      8px 9px;

    font-size:
      9px;
  }


  .player-actions {
    display:
      none;
  }

}

</style>

</head>

<body>


<section id="authScreen">

  <div class="auth-brand">

    <div class="auth-logo">
      VEEB.
    </div>

    <div class="auth-big">
      MUSIC
      <br>
      THAT
      <br>
      MOVES.
    </div>

    <div class="auth-foot">
      Your library, your playlists, your taste.
      Built around what you actually want to hear.
    </div>

  </div>


  <div class="auth-panel">

    <div class="auth-box">

      <div class="auth-eyebrow">
        PRIVATE LISTENING
      </div>

      <h2 class="auth-title">
        Welcome in.
      </h2>


      <div class="auth-tabs">

        <button
          id="loginTab"
          class="auth-tab active"
        >
          LOG IN
        </button>

        <button
          id="registerTab"
          class="auth-tab"
        >
          CREATE ACCOUNT
        </button>

      </div>


      <form id="authForm">

        <div class="field">

          <label>
            EMAIL
          </label>

          <input
            id="emailInput"
            type="email"
            autocomplete="email"
            required
          >

        </div>


        <div class="field">

          <label>
            PASSWORD
          </label>

          <input
            id="passwordInput"
            type="password"
            autocomplete="current-password"
            required
          >

        </div>


        <button
          id="authSubmit"
          class="auth-submit"
          type="submit"
        >
          LOG IN
        </button>


        <div
          id="authError"
          class="auth-error"
        ></div>

      </form>

    </div>

  </div>

</section>



<section
  id="appScreen"
  class="hidden"
>

  <header class="topbar">

    <div class="logo">
      VEEB
      <span class="logo-dot"></span>
    </div>


    <div class="top-actions">

      <span
        id="userEmail"
        class="user-email"
      ></span>

      <button
        id="homeButton"
        class="small-button active"
      >
        HOME
      </button>

      <button
        id="savedButton"
        class="small-button"
      >
        FAVOURITES
      </button>

      <button
        id="playlistsButton"
        class="small-button"
      >
        PLAYLISTS
      </button>

      <button
        id="logoutButton"
        class="small-button"
      >
        LOG OUT
      </button>

    </div>

  </header>


  <main class="shell">

    <section class="hero">

      <div class="kicker">
        YOUR MUSIC. YOUR TASTE.
      </div>

      <h1>
        Find something
        <br>
        worth hearing.
      </h1>


      <form
        id="searchForm"
        class="search"
      >

        <input
          id="searchInput"
          placeholder="Search songs, artists, albums..."
          autocomplete="off"
        >

        <button type="submit">
          SEARCH
        </button>

      </form>

    </section>


    <section
      id="homeView"
      class="home-view"
    >

      <div class="home-section">
        <div class="home-section-head">
          <div>
            <div class="section-label">FOR YOU</div>
            <h2>Made around your taste.</h2>
          </div>
          <p id="homeStatus">Learning what you like.</p>
        </div>
        <div id="homeRecommendations" class="home-track-grid"></div>
      </div>

      <div class="home-section">
        <div class="home-section-head">
          <div>
            <div class="section-label">ARTISTS</div>
            <h2>More from your orbit.</h2>
          </div>
        </div>
        <div id="homeArtists" class="taste-grid"></div>
      </div>

      <div class="home-section">
        <div class="home-section-head">
          <div>
            <div class="section-label">GENRES</div>
            <h2>Keep digging.</h2>
          </div>
        </div>
        <div id="homeGenres" class="taste-grid"></div>
      </div>

      <div class="home-section">
        <div class="home-section-head">
          <div>
            <div class="section-label">YOUR LIBRARY</div>
            <h2>Playlists & favourites.</h2>
          </div>
          <button id="createPlaylistButton" class="small-button">+ NEW PLAYLIST</button>
        </div>
        <div id="homePlaylists" class="playlist-grid"></div>
      </div>

    </section>


    <section
      id="listView"
      class="hidden"
    >

      <div
        id="sectionLabel"
        class="section-label"
      >
        DISCOVER
      </div>

      <div id="results">

        <div class="empty">
          Your music will appear here.
        </div>

      </div>

    </section>

  </main>


  <section
    id="player"
    class="player hidden"
  >

    <div class="player-up-hint">
      <i class="ph ph-caret-up"></i>
      NOW PLAYING
    </div>

    <div
      id="playerSwipeSurface"
      class="player-swipe-surface"
    >

    <div class="now" id="miniNow">

      <img
        id="nowArt"
        alt=""
      >

      <div>

        <div
          id="nowTitle"
          class="now-title"
        ></div>

        <div
          id="nowArtist"
          class="now-artist"
        ></div>

      </div>

    </div>


    <div class="player-center">

      <div class="controls">

        <button
          id="dislikeButton"
          class="control"
        >
          <i class="ph ph-thumbs-down"></i>
        </button>

        <button
          id="previousButton"
          class="control"
        >
          <i class="ph ph-skip-back"></i>
        </button>

        <button
          id="playButton"
          class="play"
        >
          <i class="ph ph-play"></i>
        </button>

        <button
          id="nextButton"
          class="control"
        >
          <i class="ph ph-skip-forward"></i>
        </button>

        <button
          id="likeButton"
          class="control"
        >
          <i class="ph ph-heart"></i>
        </button>

      </div>


      <div class="progress">

        <span id="currentTime">
          0:00
        </span>

        <input
          id="seek"
          type="range"
          min="0"
          max="0"
          value="0"
        >

        <span id="totalTime">
          0:00
        </span>

      </div>

    </div>


    <div class="player-actions">

      <button
        id="saveButton"
        class="small-button"
      >
        FAVOURITE
      </button>

      <button
        id="playlistButton"
        class="small-button"
      >
        + PLAYLIST
      </button>

    </div>

    </div>

  </section>

</section>


<section
  id="expandedPlayer"
  class="expanded-player"
  aria-hidden="true"
>
  <div class="expanded-player-shell">
    <div class="expanded-handle-row">
      <div class="expanded-spacer"></div>
      <div class="expanded-handle"></div>
      <button
        id="expandedCloseButton"
        class="expanded-close"
        aria-label="Close now playing"
      >
        <i class="ph ph-caret-down"></i>
      </button>
    </div>

    <div class="expanded-content">
      <img
        id="expandedArt"
        class="expanded-art"
        alt=""
      >

      <div class="expanded-meta-row">
        <div>
          <div id="expandedTitle" class="expanded-title"></div>
          <div id="expandedArtist" class="expanded-artist"></div>
          <div id="expandedAlbum" class="expanded-album"></div>
        </div>

        <button
          id="expandedLikeButton"
          class="expanded-like"
          aria-label="Favourite track"
        >
          <i class="ph ph-heart"></i>
        </button>
      </div>

      <div class="expanded-progress">
        <input
          id="expandedSeek"
          type="range"
          min="0"
          max="0"
          value="0"
        >
        <div class="expanded-times">
          <span id="expandedCurrentTime">0:00</span>
          <span id="expandedTotalTime">0:00</span>
        </div>
      </div>

      <div class="expanded-controls">
        <button id="expandedDislikeButton" class="expanded-control" aria-label="Less like this">
          <i class="ph ph-thumbs-down"></i>
        </button>
        <button id="expandedPreviousButton" class="expanded-control" aria-label="Previous track">
          <i class="ph ph-skip-back"></i>
        </button>
        <button id="expandedPlayButton" class="expanded-play" aria-label="Play or pause">
          <i class="ph ph-play"></i>
        </button>
        <button id="expandedNextButton" class="expanded-control" aria-label="Next track">
          <i class="ph ph-skip-forward"></i>
        </button>
        <button id="expandedLikeControl" class="expanded-control" aria-label="Favourite track">
          <i class="ph ph-heart"></i>
        </button>
      </div>

      <div class="expanded-bottom-actions">
        <button id="expandedPlaylistButton" class="small-button">+ PLAYLIST</button>
        <button id="expandedFavouriteButton" class="small-button">FAVOURITE</button>
      </div>
    </div>
  </div>
</section>


<audio
  id="audioEngine"
  preload="auto"
  playsinline
></audio>

<div
  id="playlistModal"
  class="modal-backdrop hidden"
>
  <div class="modal-card">
    <div class="modal-head">
      <h3>ADD TO PLAYLIST</h3>
      <button id="playlistModalClose" class="modal-close" aria-label="Close">
        <i class="ph ph-x"></i>
      </button>
    </div>

    <div id="playlistChoices"></div>

    <div class="modal-create">
      <input id="newPlaylistName" placeholder="New playlist name">
      <button id="newPlaylistCreate">CREATE</button>
    </div>
  </div>
</div>

<div
  id="toast"
  class="toast"
></div>


<script>

const state = {
  authMode: "login",
  user: null,

  tracks: [],
  currentTrack: null,

  queue: [],
  history: [],
  savedIds: new Set(),
  playlists: [],
  homeRecommendations: [],
  queueRefreshToken: 0,
  prefetchRequested: new Set(),
  visiblePrefetchGeneration: 0,
  visiblePrefetchTimers: [],
  playerGestureBusy: false,
  playbackGeneration: 0,
  playbackPendingGeneration: 0,
  expectedAudioSrc: "",
  mediaRecoveryGeneration: 0
};


const audio =
  document.getElementById(
    "audioEngine"
  );

audio.preload =
  "auto";

audio.disableRemotePlayback =
  true;


const authScreen =
  document.getElementById(
    "authScreen"
  );

const appScreen =
  document.getElementById(
    "appScreen"
  );


async function initialise() {

  const response =
    await fetch(
      "/api/auth/status"
    );

  const result =
    await response.json();


  if (
    result.authenticated
  ) {

    showApp(
      result.user
    );

    void fetch("/api/resolver/wake", { method: "POST", keepalive: true });
    await loadHome();

  } else {

    showAuth();

  }

}


function showAuth() {

  authScreen.classList.remove(
    "hidden"
  );

  appScreen.classList.add(
    "hidden"
  );

}


function showApp(
  user
) {

  state.user =
    user;


  authScreen.classList.add(
    "hidden"
  );

  appScreen.classList.remove(
    "hidden"
  );


  document.getElementById(
    "userEmail"
  ).textContent =
    user.email;

}


document.getElementById(
  "loginTab"
).onclick =
  () => setAuthMode(
    "login"
  );


document.getElementById(
  "registerTab"
).onclick =
  () => setAuthMode(
    "register"
  );


function setAuthMode(
  mode
) {

  state.authMode =
    mode;


  document.getElementById(
    "loginTab"
  ).classList.toggle(
    "active",
    mode === "login"
  );


  document.getElementById(
    "registerTab"
  ).classList.toggle(
    "active",
    mode === "register"
  );


  document.getElementById(
    "authSubmit"
  ).textContent =
    mode === "login"
      ? "LOG IN"
      : "CREATE ACCOUNT";


  document.getElementById(
    "passwordInput"
  ).autocomplete =
    mode === "login"
      ? "current-password"
      : "new-password";


  document.getElementById(
    "authError"
  ).textContent =
    "";

}


document.getElementById(
  "authForm"
).addEventListener(
  "submit",
  async event => {

    event.preventDefault();


    const email =
      document.getElementById(
        "emailInput"
      ).value.trim();


    const password =
      document.getElementById(
        "passwordInput"
      ).value;


    const endpoint =
      state.authMode === "login"
        ? "/api/auth/login"
        : "/api/auth/register";


    const button =
      document.getElementById(
        "authSubmit"
      );


    button.disabled =
      true;


    document.getElementById(
      "authError"
    ).textContent =
      "";


    try {

      const response =
        await fetch(
          endpoint,
          {
            method:
              "POST",

            headers: {
              "Content-Type":
                "application/json"
            },

            body:
              JSON.stringify({
                email,
                password
              })
          }
        );


      const responseText =
        await response.text();


      let result = null;


      try {
        result = JSON.parse(
          responseText
        );
      } catch (_) {
        console.error(
          "Non-JSON auth response:",
          response.status,
          responseText
        );

        throw new Error(
          "Veeb server error (" + response.status + "). Check Worker logs."
        );
      }


      if (!response.ok) {

        throw new Error(
          result.error ||
          "Authentication failed"
        );

      }


      showApp(
        result.user
      );


      await loadHome();


    } catch (error) {

      document.getElementById(
        "authError"
      ).textContent =
        error.message;


    } finally {

      button.disabled =
        false;

    }

  }
);


document.getElementById(
  "logoutButton"
).onclick =
  async () => {

    await fetch(
      "/api/auth/logout",
      {
        method:
          "POST"
      }
    );


    location.reload();

  };


document.getElementById(
  "searchForm"
).addEventListener(
  "submit",
  async event => {

    event.preventDefault();


    const query =
      document.getElementById(
        "searchInput"
      ).value.trim();


    if (!query) {
      return;
    }


    showListView(
      "SEARCH RESULTS"
    );


    setResultsMessage(
      "SEARCHING..."
    );


    const response =
      await fetch(
        "/api/search?q="
        + encodeURIComponent(
          query
        )
      );


    if (
      response.status === 401
    ) {

      location.reload();

      return;

    }


    const tracks =
      await response.json();


    state.tracks =
      tracks;


    renderTracks(
      tracks
    );

    scheduleVisiblePrefetches(Array.isArray(tracks) ? tracks : []);

  }
);


function setNavActive(id) {
  [
    "homeButton",
    "savedButton",
    "playlistsButton"
  ].forEach(buttonId => {
    const button = document.getElementById(buttonId);
    if (button) {
      button.classList.toggle("active", buttonId === id);
    }
  });
}


function showHomeView() {
  document.getElementById("homeView").classList.remove("hidden");
  document.getElementById("listView").classList.add("hidden");
  setNavActive("homeButton");
}


function showListView(label) {
  document.getElementById("homeView").classList.add("hidden");
  document.getElementById("listView").classList.remove("hidden");
  if (label) {
    setSection(label);
  }
}


async function fetchJson(url, options) {
  const response = await fetch(url, options);

  if (response.status === 401) {
    location.reload();
    throw new Error("Unauthorized");
  }

  let data = null;
  try {
    data = await response.json();
  } catch (_) {}

  if (!response.ok) {
    throw new Error(data?.error || "Request failed");
  }

  return data;
}


async function loadLibraryState() {
  const [saved, playlists] = await Promise.all([
    fetchJson("/api/saved").catch(() => []),
    fetchJson("/api/playlists").catch(() => [])
  ]);

  state.savedIds = new Set(
    (saved || []).map(track => track.id)
  );
  state.playlists = playlists || [];
  updateFavouriteUI();

  return { saved, playlists };
}


async function loadHome() {
  showHomeView();

  const status = document.getElementById("homeStatus");
  status.textContent = "Building your home...";

  const [recommendations, taste, library] = await Promise.all([
    fetchJson("/api/recommendations").catch(() => []),
    fetchJson("/api/taste").catch(() => ({ artists: [], genres: [], tracks: [] })),
    loadLibraryState()
  ]);

  state.homeRecommendations = Array.isArray(recommendations)
    ? recommendations
    : [];

  state.queue = state.homeRecommendations.slice();

  scheduleVisiblePrefetches(state.homeRecommendations);

  renderHomeTrackCards(
    document.getElementById("homeRecommendations"),
    state.homeRecommendations.slice(0, 12)
  );

  renderTasteCards(
    document.getElementById("homeArtists"),
    (taste.artists || []).slice(0, 8),
    "artist"
  );

  renderTasteCards(
    document.getElementById("homeGenres"),
    (taste.genres || []).slice(0, 8),
    "genre"
  );

  renderPlaylistCards(
    library.playlists || [],
    library.saved || []
  );

  status.textContent = state.homeRecommendations.length
    ? "Personalised from your listening."
    : "Play, favourite and skip tracks to shape this page.";
}


function renderHomeTrackCards(container, tracks) {
  container.innerHTML = "";

  if (!tracks.length) {
    container.innerHTML = '<div class="library-empty">No recommendations yet. Search and play a few tracks first.</div>';
    return;
  }

  tracks.forEach((track, index) => {
    const card = document.createElement("button");
    card.className = "home-track-card";
    card.innerHTML = \`
      <img src="\${escapeHtml(track.artwork || "")}" alt="">
      <div class="home-track-card-copy">
        <div class="home-track-card-title">\${escapeHtml(track.title || "")}</div>
        <div class="home-track-card-sub">\${escapeHtml(track.artist || "")}</div>
      </div>
    \`;

    card.onpointerenter = () => prefetchTrack(track, true);
    card.onfocus = () => prefetchTrack(track, true);
    card.onpointerdown = () => prefetchTrack(track, true);
    card.onclick = () => {
      state.queue = tracks.slice(index + 1);
      void playTrack(track, { refreshQueue: false });
      void appendRadioQueue(track.id);
    };

    container.appendChild(card);
  });
}


function renderTasteCards(container, items, type) {
  container.innerHTML = "";

  if (!items.length) {
    container.innerHTML = '<div class="library-empty">Still learning this part of your taste.</div>';
    return;
  }

  items.forEach(item => {
    const name = type === "artist"
      ? (item.artistName || item.artistKey || "Artist")
      : (item.genre || "Genre");

    const score = Number(item.score || 0);
    const card = document.createElement("button");
    card.className = "taste-card";
    card.innerHTML = \`
      <i class="ph \${type === "artist" ? "ph-microphone-stage" : "ph-waveform"}"></i>
      <div>
        <strong>\${escapeHtml(name)}</strong>
        <span>\${type === "artist" ? "ARTIST" : "GENRE"}\${score ? " · " + Math.round(score) : ""}</span>
      </div>
    \`;

    card.onclick = () => searchByTerm(name, type === "artist" ? "ARTIST" : "GENRE");
    container.appendChild(card);
  });
}


function renderPlaylistCards(playlists, savedTracks) {
  const container = document.getElementById("homePlaylists");
  container.innerHTML = "";

  const favourites = document.createElement("button");
  favourites.className = "playlist-card";
  favourites.innerHTML = \`
    <i class="ph ph-heart"></i>
    <div>
      <strong>Favourites</strong>
      <span>\${savedTracks.length} SAVED TRACK\${savedTracks.length === 1 ? "" : "S"}</span>
    </div>
  \`;
  favourites.onclick = loadFavourites;
  container.appendChild(favourites);

  playlists.slice(0, 7).forEach(playlist => {
    const card = document.createElement("button");
    card.className = "playlist-card";
    card.innerHTML = \`
      <i class="ph ph-playlist"></i>
      <div>
        <strong>\${escapeHtml(playlist.name || "Playlist")}</strong>
        <span>PLAYLIST</span>
      </div>
    \`;
    card.onclick = () => loadPlaylist(playlist.id, playlist.name);
    container.appendChild(card);
  });
}


async function searchByTerm(term, label) {
  const input = document.getElementById("searchInput");
  input.value = term;
  showListView(label + " · " + term.toUpperCase());
  setNavActive("");
  setResultsMessage("SEARCHING...");

  const tracks = await fetchJson(
    "/api/search?q=" + encodeURIComponent(term)
  ).catch(error => {
    setResultsMessage(error.message || "SEARCH FAILED");
    return [];
  });

  state.tracks = tracks;
  renderTracks(tracks);
  scheduleVisiblePrefetches(tracks);
}


async function loadFavourites() {
  showListView("FAVOURITES");
  setNavActive("savedButton");
  setResultsMessage("LOADING...");

  const tracks = await fetchJson("/api/saved").catch(() => []);
  state.savedIds = new Set(tracks.map(track => track.id));
  state.tracks = tracks;
  renderTracks(tracks);
  scheduleVisiblePrefetches(tracks);
  updateFavouriteUI();
}


async function loadPlaylists() {
  showListView("PLAYLISTS");
  setNavActive("playlistsButton");

  const playlists = await fetchJson("/api/playlists").catch(() => []);
  state.playlists = playlists;

  const container = document.getElementById("results");
  container.innerHTML = "";
  container.className = "playlist-grid";

  const create = document.createElement("button");
  create.className = "playlist-card";
  create.innerHTML = '<i class="ph ph-plus"></i><div><strong>New playlist</strong><span>CREATE</span></div>';
  create.onclick = () => openPlaylistModal(true);
  container.appendChild(create);

  playlists.forEach(playlist => {
    const card = document.createElement("button");
    card.className = "playlist-card";
    card.innerHTML = \`
      <i class="ph ph-playlist"></i>
      <div><strong>\${escapeHtml(playlist.name || "Playlist")}</strong><span>OPEN PLAYLIST</span></div>
    \`;
    card.onclick = () => loadPlaylist(playlist.id, playlist.name);
    container.appendChild(card);
  });
}


async function loadPlaylist(id, name) {
  showListView((name || "PLAYLIST").toUpperCase());
  setNavActive("playlistsButton");
  setResultsMessage("LOADING...");

  const payload = await fetchJson(
    "/api/playlists/" + encodeURIComponent(id) + "/tracks"
  ).catch(() => ({ tracks: [] }));

  const tracks = payload.tracks || [];
  state.tracks = tracks;
  state.queue = tracks.slice();
  renderTracks(tracks);
  scheduleVisiblePrefetches(tracks);
}


document.getElementById("homeButton").onclick = loadHome;
document.getElementById("savedButton").onclick = loadFavourites;
document.getElementById("playlistsButton").onclick = loadPlaylists;
document.getElementById("createPlaylistButton").onclick = () => openPlaylistModal(true);


function renderTracks(
  tracks
) {

  const container =
    document.getElementById(
      "results"
    );


  container.className =
    "";


  container.innerHTML =
    "";


  if (
    !Array.isArray(tracks) ||
    !tracks.length
  ) {

    setResultsMessage(
      "Nothing here yet."
    );

    return;

  }


  tracks.forEach(
    (
      track,
      index
    ) => {

      const row =
        document.createElement(
          "button"
        );


      row.className =
        "track-row";


      row.innerHTML =
        \`

          <div class="track-num">
            \${String(
              index + 1
            ).padStart(
              2,
              "0"
            )}
          </div>

          <img
            src="\${escapeHtml(
              track.artwork || ""
            )}"
            alt=""
          >

          <div>

            <div class="track-title">
              \${escapeHtml(
                track.title || ""
              )}
            </div>

            <div class="track-sub">
              \${escapeHtml(
                (track.artist || "")
                + (track.album ? " · " + track.album : "")
                + (track.veebReason ? " · " + track.veebReason : "")
              )}
            </div>

          </div>

          <div class="track-play">
            <i class="ph ph-play"></i>
          </div>

        \`;


      row.onpointerenter =
        () => prefetchTrack(
          track,
          true
        );

      row.onfocus =
        () => prefetchTrack(
          track,
          true
        );

      row.onpointerdown =
        () => prefetchTrack(
          track,
          true
        );


      row.onclick =
        () => playTrack(
          track
        );


      container.appendChild(
        row
      );

    }
  );

}


async function playTrack(
  track,
  options
) {

  const opts =
    options || {};

  const playbackGeneration =
    ++state.playbackGeneration;

  state.playbackPendingGeneration =
    playbackGeneration;

  state.mediaRecoveryGeneration =
    0;

  cancelVisiblePrefetchTimers();

  if (
    state.currentTrack &&
    state.currentTrack.id !== track.id
  ) {

    state.history.push(
      state.currentTrack
    );

    if (!opts.suppressTransitionSignal) {
      void sendListeningEvent(
        "skip"
      );
    }

  }

  state.currentTrack =
    track;

  document.getElementById(
    "player"
  ).classList.remove(
    "hidden"
  );

  document.getElementById(
    "nowTitle"
  ).textContent =
    track.title || "";

  document.getElementById(
    "nowArtist"
  ).textContent =
    (track.artist || "") + " · PREPARING AUDIO";

  document.getElementById(
    "nowArt"
  ).src =
    track.artwork || "";

  syncExpandedPlayerTrack(
    track
  );

  updateFavouriteUI();

  setMediaSession(
    track
  );

  document.getElementById(
    "playButton"
  ).innerHTML =
    '<i class="ph ph-spinner-gap veeb-spin"></i>';

  document.getElementById(
    "expandedPlayButton"
  ).innerHTML =
    '<i class="ph ph-spinner-gap veeb-spin"></i>';

  const basePlaybackUrl =
    "/api/audio/"
    + encodeURIComponent(
      track.id
    )
    + "?transport=v25&generation="
    + playbackGeneration;

  state.expectedAudioSrc =
    new URL(
      basePlaybackUrl,
      window.location.href
    ).href;

  audio.src =
    basePlaybackUrl;

  audio.load();

  // First play() happens immediately inside the user's click/swipe activation.
  // If a stale request is aborted because the user changes track again, that
  // AbortError is normal navigation and must never surface as PLAYBACK FAILED.
  let playError = null;
  let started = false;

  for (
    let attempt = 0;
    attempt < 3;
    attempt += 1
  ) {

    if (
      playbackGeneration !== state.playbackGeneration ||
      state.currentTrack?.id !== track.id
    ) {
      return;
    }

    if (attempt > 0) {
      await new Promise(
        resolve => setTimeout(
          resolve,
          attempt === 1 ? 350 : 900
        )
      );

      if (
        playbackGeneration !== state.playbackGeneration ||
        state.currentTrack?.id !== track.id
      ) {
        return;
      }

      const retryUrl =
        basePlaybackUrl
        + "&retry="
        + attempt;

      state.expectedAudioSrc =
        new URL(
          retryUrl,
          window.location.href
        ).href;

      audio.src =
        retryUrl;

      audio.load();
    }

    try {
      await audio.play();
      started = true;
      playError = null;
      break;
    } catch (error) {
      playError = error;

      console.error(
        "Audio play() attempt failed:",
        attempt + 1,
        error?.name,
        error?.message,
        error
      );

      if (
        error?.name === "AbortError" ||
        playbackGeneration !== state.playbackGeneration ||
        state.currentTrack?.id !== track.id
      ) {
        return;
      }

      if (error?.name === "NotAllowedError") {
        break;
      }
    }
  }

  if (
    playbackGeneration === state.playbackPendingGeneration
  ) {
    state.playbackPendingGeneration =
      0;
  }

  // Persist track metadata in parallel. This must not delay playback.
  void fetch(
    "/api/tracks",
    {
      method:
        "POST",

      headers: {
        "Content-Type":
          "application/json"
      },

      body:
        JSON.stringify(
          track
        )
    }
  ).catch(
    error =>
      console.error(
        "Track persistence failed:",
        error
      )
  );

  if (started) {
    await sendListeningEvent(
      "play"
    );

    if (opts.refreshQueue !== false) {
      refreshQueueFromSeed(
        track.id
      );
    }

    return;
  }

  if (
    playbackGeneration !== state.playbackGeneration ||
    state.currentTrack?.id !== track.id
  ) {
    return;
  }

  document.getElementById(
    "playButton"
  ).innerHTML =
    '<i class="ph ph-play"></i>';

  document.getElementById(
    "expandedPlayButton"
  ).innerHTML =
    '<i class="ph ph-play"></i>';

  if (playError?.name === "NotAllowedError") {
    showToast(
      "TAP PLAY TO CONTINUE"
    );
  } else {
    // Three real attempts have failed for the current track. Avoid the old
    // alarming PLAYBACK FAILED toast for normal source swaps/AbortErrors.
    showToast(
      "AUDIO DIDN'T START · TAP PLAY TO RETRY"
    );
  }

}



document.getElementById(
  "playButton"
).onclick =
  async () => {

    if (!state.currentTrack) {
      return;
    }


    if (audio.paused) {

      await audio.play();

    } else {

      audio.pause();

    }

  };


async function toggleFavourite(track) {
  if (!track) {
    return;
  }

  const isSaved = state.savedIds.has(track.id);

  if (isSaved) {
    await fetchJson(
      "/api/unsave",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trackId: track.id })
      }
    );

    state.savedIds.delete(track.id);
    showToast("REMOVED FROM FAVOURITES");
  } else {
    await fetchJson(
      "/api/save",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(track)
      }
    );

    state.savedIds.add(track.id);
    void sendListeningEvent("like");
    showToast("ADDED TO FAVOURITES");
  }

  updateFavouriteUI();
}


function updateFavouriteUI() {
  const saved = !!state.currentTrack && state.savedIds.has(state.currentTrack.id);
  const heart = document.getElementById("likeButton");
  const save = document.getElementById("saveButton");
  const expandedHeart = document.getElementById("expandedLikeButton");
  const expandedHeartControl = document.getElementById("expandedLikeControl");
  const expandedFavourite = document.getElementById("expandedFavouriteButton");
  const iconHtml = saved
    ? '<i class="ph-fill ph-heart"></i>'
    : '<i class="ph ph-heart"></i>';

  heart.classList.toggle("is-favourite", saved);
  heart.innerHTML = iconHtml;

  save.textContent = saved
    ? "FAVOURITED"
    : "FAVOURITE";

  expandedHeart.classList.toggle("is-favourite", saved);
  expandedHeart.innerHTML = iconHtml;
  expandedHeartControl.classList.toggle("is-favourite", saved);
  expandedHeartControl.innerHTML = iconHtml;
  expandedFavourite.textContent = saved
    ? "FAVOURITED"
    : "FAVOURITE";
}


function syncExpandedPlayerTrack(track) {
  if (!track) {
    return;
  }

  document.getElementById("expandedArt").src = track.artwork || "";
  document.getElementById("expandedTitle").textContent = track.title || "";
  document.getElementById("expandedArtist").textContent = track.artist || "";
  document.getElementById("expandedAlbum").textContent = track.album || "";
}


document.getElementById("likeButton").onclick = async () => {
  await toggleFavourite(state.currentTrack);
};


document.getElementById("dislikeButton").onclick = async () => {
  await sendListeningEvent("dislike");
  showToast("LESS LIKE THIS");
  await playNext(true);
};


document.getElementById("saveButton").onclick = async () => {
  await toggleFavourite(state.currentTrack);
};


document.getElementById("nextButton").onclick = async () => {
  await animatePlayerNavigation("next", false);
};


document.getElementById("previousButton").onclick = async () => {
  await animatePlayerNavigation("previous", true);
};


const miniPlayer = document.getElementById("player");
const playerSwipeSurface = document.getElementById("playerSwipeSurface");
const expandedPlayer = document.getElementById("expandedPlayer");

const miniGesture = {
  active: false,
  pointerId: null,
  startX: 0,
  startY: 0,
  lastX: 0,
  lastY: 0,
  lastTime: 0,
  velocityX: 0,
  velocityY: 0,
  axis: null,
  direction: null,
  preview: null
};

const expandedGesture = {
  active: false,
  pointerId: null,
  startY: 0,
  lastY: 0,
  lastTime: 0,
  velocityY: 0
};


function clampNumber(value, min, max) {
  return Math.min(max, Math.max(min, value));
}


function getNavigationPreviewTrack(direction) {
  if (direction === "next") {
    return state.queue.find(
      track => track?.id && track.id !== state.currentTrack?.id
    ) || null;
  }

  if (direction === "previous") {
    return state.history.length
      ? state.history[state.history.length - 1]
      : null;
  }

  return null;
}


function removeSwipePreview() {
  if (miniGesture.preview) {
    miniGesture.preview.remove();
    miniGesture.preview = null;
  }
}


function createSwipePreview(direction) {
  const track = getNavigationPreviewTrack(direction);

  removeSwipePreview();

  if (!track) {
    return null;
  }

  const preview = playerSwipeSurface.cloneNode(true);
  preview.removeAttribute("id");
  preview.classList.add("player-swipe-preview");
  preview.setAttribute("aria-hidden", "true");

  preview.querySelectorAll("[id]").forEach(
    element => element.removeAttribute("id")
  );

  const art = preview.querySelector(".now img");
  const title = preview.querySelector(".now-title");
  const artist = preview.querySelector(".now-artist");

  if (art) {
    art.src = track.artwork || "";
  }

  if (title) {
    title.textContent = track.title || "";
  }

  if (artist) {
    artist.textContent = track.artist || "";
  }

  const heart = preview.querySelector(".controls .control:last-child");
  if (heart) {
    const saved = state.savedIds.has(track.id);
    heart.classList.toggle("is-favourite", saved);
    heart.innerHTML = saved
      ? '<i class="ph-fill ph-heart"></i>'
      : '<i class="ph ph-heart"></i>';
  }

  preview.style.transition = "none";
  miniPlayer.appendChild(preview);
  miniGesture.preview = preview;

  prefetchTrack(track, true);

  return preview;
}


function positionSwipeDeck(dx, direction) {
  const width = Math.max(1, miniPlayer.getBoundingClientRect().width);
  const preview = miniGesture.preview;

  playerSwipeSurface.style.transform =
    "translate3d(" + dx + "px,0,0)";

  if (preview) {
    const base = direction === "next" ? width : -width;
    preview.style.transform =
      "translate3d(" + (base + dx) + "px,0,0)";
  }
}


function resetSwipeDeck(animated) {
  const duration = animated ? 220 : 0;
  const transition = duration
    ? "transform " + duration + "ms cubic-bezier(.22,1,.36,1)"
    : "none";

  playerSwipeSurface.style.transition = transition;
  playerSwipeSurface.style.transform = "translate3d(0,0,0)";

  if (miniGesture.preview) {
    miniGesture.preview.style.transition = transition;
    const direction = miniGesture.direction || "next";
    const width = Math.max(1, miniPlayer.getBoundingClientRect().width);
    miniGesture.preview.style.transform =
      "translate3d(" + (direction === "next" ? width : -width) + "px,0,0)";
  }

  setTimeout(
    () => {
      playerSwipeSurface.style.transition = "";
      playerSwipeSurface.style.transform = "";
      removeSwipePreview();
    },
    duration + 25
  );
}


async function performNavigation(direction, suppressTransitionSignal) {
  if (direction === "next") {
    await playNext(!!suppressTransitionSignal);
    return;
  }

  await playPrevious();
}


function commitSwipeNavigation(direction, suppressTransitionSignal, velocityX) {
  if (state.playerGestureBusy) {
    return Promise.resolve();
  }

  const track = getNavigationPreviewTrack(direction);
  if (!track) {
    resetSwipeDeck(true);
    return Promise.resolve();
  }

  state.playerGestureBusy = true;
  prefetchTrack(track, true);

  if (!miniGesture.preview || miniGesture.direction !== direction) {
    miniGesture.direction = direction;
    createSwipePreview(direction);
  }

  const width = Math.max(1, miniPlayer.getBoundingClientRect().width);
  const speed = Math.abs(Number(velocityX || 0));
  const duration = Math.round(clampNumber(225 - speed * 90, 120, 225));
  const destination = direction === "next" ? -width : width;
  const transition =
    "transform " + duration + "ms cubic-bezier(.22,1,.36,1)";

  playerSwipeSurface.style.transition = transition;
  playerSwipeSurface.style.transform =
    "translate3d(" + destination + "px,0,0)";

  if (miniGesture.preview) {
    miniGesture.preview.style.transition = transition;
    miniGesture.preview.style.transform = "translate3d(0,0,0)";
  }

  try {
    if (navigator.vibrate) {
      navigator.vibrate(8);
    }
  } catch (_) {}

  return new Promise(resolve => {
    setTimeout(
      () => {
        const preview = miniGesture.preview;

        playerSwipeSurface.style.transition = "none";
        playerSwipeSurface.style.transform = "translate3d(0,0,0)";
        playerSwipeSurface.style.opacity = "0";

        void performNavigation(
          direction,
          suppressTransitionSignal
        ).finally(() => {
          state.playerGestureBusy = false;
          resolve();
        });

        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            playerSwipeSurface.style.transition = "opacity 130ms ease";
            playerSwipeSurface.style.opacity = "1";

            if (preview) {
              preview.style.transition = "opacity 130ms ease";
              preview.style.opacity = "0";
            }

            setTimeout(() => {
              playerSwipeSurface.style.transition = "";
              playerSwipeSurface.style.opacity = "";
              removeSwipePreview();
            }, 150);
          });
        });
      },
      duration
    );
  });
}


async function animatePlayerNavigation(direction, suppressTransitionSignal) {
  if (state.playerGestureBusy) {
    return;
  }

  const track = getNavigationPreviewTrack(direction);

  if (!track) {
    await performNavigation(direction, suppressTransitionSignal);
    return;
  }

  miniGesture.direction = direction;
  createSwipePreview(direction);
  positionSwipeDeck(0, direction);

  await commitSwipeNavigation(
    direction,
    suppressTransitionSignal,
    .7
  );
}


function openExpandedPlayer() {
  if (!state.currentTrack) {
    return;
  }

  syncExpandedPlayerTrack(state.currentTrack);
  updateFavouriteUI();

  const expandedSeek = document.getElementById("expandedSeek");
  expandedSeek.max = audio.duration || 0;
  expandedSeek.value = audio.currentTime || 0;
  document.getElementById("expandedCurrentTime").textContent = formatTime(audio.currentTime);
  document.getElementById("expandedTotalTime").textContent = formatTime(audio.duration);

  expandedPlayer.style.transform = "";
  expandedPlayer.classList.add("open");
  expandedPlayer.setAttribute("aria-hidden", "false");
  document.body.classList.add("expanded-player-open");
}


function closeExpandedPlayer() {
  expandedPlayer.classList.remove("dragging");
  expandedPlayer.style.transform = "";
  expandedPlayer.classList.remove("open");
  expandedPlayer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("expanded-player-open");
}


miniPlayer.addEventListener(
  "pointerdown",
  event => {
    if (
      event.pointerType === "mouse" &&
      event.button !== 0
    ) {
      return;
    }

    if (event.target.closest("button,input")) {
      return;
    }

    if (state.playerGestureBusy) {
      return;
    }

    miniGesture.active = true;
    miniGesture.pointerId = event.pointerId;
    miniGesture.startX = event.clientX;
    miniGesture.startY = event.clientY;
    miniGesture.lastX = event.clientX;
    miniGesture.lastY = event.clientY;
    miniGesture.lastTime = performance.now();
    miniGesture.velocityX = 0;
    miniGesture.velocityY = 0;
    miniGesture.axis = null;
    miniGesture.direction = null;

    miniPlayer.classList.add("gesture-active");

    try {
      miniPlayer.setPointerCapture(event.pointerId);
    } catch (_) {}

    if (!state.queue.length && state.currentTrack?.id) {
      void ensureQueue();
    }
  },
  { passive: false }
);


miniPlayer.addEventListener(
  "pointermove",
  event => {
    if (
      !miniGesture.active ||
      event.pointerId !== miniGesture.pointerId
    ) {
      return;
    }

    const now = performance.now();
    const dt = Math.max(1, now - miniGesture.lastTime);
    const dx = event.clientX - miniGesture.startX;
    const dy = event.clientY - miniGesture.startY;
    const instantaneousX = (event.clientX - miniGesture.lastX) / dt;
    const instantaneousY = (event.clientY - miniGesture.lastY) / dt;

    miniGesture.velocityX =
      miniGesture.velocityX * .65 + instantaneousX * .35;
    miniGesture.velocityY =
      miniGesture.velocityY * .65 + instantaneousY * .35;
    miniGesture.lastX = event.clientX;
    miniGesture.lastY = event.clientY;
    miniGesture.lastTime = now;

    if (!miniGesture.axis) {
      if (Math.hypot(dx, dy) < 8) {
        return;
      }

      miniGesture.axis =
        Math.abs(dx) > Math.abs(dy) * 1.08
          ? "x"
          : "y";
    }

    if (miniGesture.axis === "x") {
      event.preventDefault();

      const direction = dx < 0 ? "next" : "previous";
      const track = getNavigationPreviewTrack(direction);

      if (miniGesture.direction !== direction) {
        miniGesture.direction = direction;
        createSwipePreview(direction);
      }

      const effectiveDx = track ? dx : dx * .24;
      positionSwipeDeck(effectiveDx, direction);
      return;
    }

    if (miniGesture.axis === "y" && dy < 0) {
      event.preventDefault();
      const lift = Math.max(-42, dy * .22);
      playerSwipeSurface.style.transition = "none";
      playerSwipeSurface.style.transform =
        "translate3d(0," + lift + "px,0)";
    }
  },
  { passive: false }
);


function finishMiniGesture(event) {
  if (
    !miniGesture.active ||
    event.pointerId !== miniGesture.pointerId
  ) {
    return;
  }

  const dx = event.clientX - miniGesture.startX;
  const dy = event.clientY - miniGesture.startY;
  const axis = miniGesture.axis;
  const direction = miniGesture.direction;
  const velocityX = miniGesture.velocityX;
  const velocityY = miniGesture.velocityY;

  miniGesture.active = false;
  miniGesture.pointerId = null;
  miniPlayer.classList.remove("gesture-active");

  try {
    miniPlayer.releasePointerCapture(event.pointerId);
  } catch (_) {}

  if (axis === "x" && direction) {
    const width = Math.max(1, miniPlayer.getBoundingClientRect().width);
    const threshold = Math.min(105, width * .22);
    const track = getNavigationPreviewTrack(direction);
    const movingCorrectWay =
      direction === "next"
        ? velocityX < -.5
        : velocityX > .5;
    const committed =
      !!track &&
      (Math.abs(dx) >= threshold || movingCorrectWay);

    if (committed) {
      void commitSwipeNavigation(
        direction,
        direction === "previous",
        velocityX
      );
      return;
    }

    resetSwipeDeck(true);
    return;
  }

  if (
    axis === "y" &&
    (-dy > 68 || velocityY < -.42)
  ) {
    resetSwipeDeck(false);
    openExpandedPlayer();
    return;
  }

  resetSwipeDeck(true);
}


miniPlayer.addEventListener("pointerup", finishMiniGesture);
miniPlayer.addEventListener("pointercancel", finishMiniGesture);


document.getElementById("miniNow").onclick = () => {
  if (!miniGesture.active && !state.playerGestureBusy) {
    openExpandedPlayer();
  }
};


document.getElementById("expandedCloseButton").onclick = closeExpandedPlayer;

document.getElementById("expandedLikeButton").onclick = async () => {
  await toggleFavourite(state.currentTrack);
};

document.getElementById("expandedLikeControl").onclick = async () => {
  await toggleFavourite(state.currentTrack);
};

document.getElementById("expandedFavouriteButton").onclick = async () => {
  await toggleFavourite(state.currentTrack);
};

document.getElementById("expandedPlaylistButton").onclick = () => {
  openPlaylistModal(false);
};

document.getElementById("expandedDislikeButton").onclick = async () => {
  await sendListeningEvent("dislike");
  showToast("LESS LIKE THIS");
  await animatePlayerNavigation("next", true);
};

document.getElementById("expandedNextButton").onclick = async () => {
  await animatePlayerNavigation("next", false);
};

document.getElementById("expandedPreviousButton").onclick = async () => {
  await animatePlayerNavigation("previous", true);
};

document.getElementById("expandedPlayButton").onclick = async () => {
  if (!state.currentTrack) {
    return;
  }

  if (audio.paused) {
    await audio.play();
  } else {
    audio.pause();
  }
};


document.getElementById("expandedSeek").oninput = event => {
  audio.currentTime = Number(event.target.value);
};


expandedPlayer.addEventListener(
  "pointerdown",
  event => {
    if (event.target.closest("button,input")) {
      return;
    }

    expandedGesture.active = true;
    expandedGesture.pointerId = event.pointerId;
    expandedGesture.startY = event.clientY;
    expandedGesture.lastY = event.clientY;
    expandedGesture.lastTime = performance.now();
    expandedGesture.velocityY = 0;
    expandedPlayer.classList.add("dragging");

    try {
      expandedPlayer.setPointerCapture(event.pointerId);
    } catch (_) {}
  },
  { passive: false }
);


expandedPlayer.addEventListener(
  "pointermove",
  event => {
    if (
      !expandedGesture.active ||
      event.pointerId !== expandedGesture.pointerId
    ) {
      return;
    }

    const dy = Math.max(0, event.clientY - expandedGesture.startY);
    const now = performance.now();
    const dt = Math.max(1, now - expandedGesture.lastTime);
    const instantaneousY = (event.clientY - expandedGesture.lastY) / dt;

    expandedGesture.velocityY =
      expandedGesture.velocityY * .65 + instantaneousY * .35;
    expandedGesture.lastY = event.clientY;
    expandedGesture.lastTime = now;

    if (dy > 0) {
      event.preventDefault();
      expandedPlayer.style.transform =
        "translate3d(0," + dy + "px,0)";
    }
  },
  { passive: false }
);


function finishExpandedGesture(event) {
  if (
    !expandedGesture.active ||
    event.pointerId !== expandedGesture.pointerId
  ) {
    return;
  }

  const dy = Math.max(0, event.clientY - expandedGesture.startY);
  const shouldClose =
    dy > 105 || expandedGesture.velocityY > .5;

  expandedGesture.active = false;
  expandedGesture.pointerId = null;
  expandedPlayer.classList.remove("dragging");

  try {
    expandedPlayer.releasePointerCapture(event.pointerId);
  } catch (_) {}

  if (shouldClose) {
    closeExpandedPlayer();
    return;
  }

  expandedPlayer.style.transform = "";
};


expandedPlayer.addEventListener("pointerup", finishExpandedGesture);
expandedPlayer.addEventListener("pointercancel", finishExpandedGesture);

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && expandedPlayer.classList.contains("open")) {
    closeExpandedPlayer();
  }
});


function prefetchTrack(track, intent) {
  const trackId = track?.id;

  if (
    !trackId ||
    state.prefetchRequested.has(trackId) ||
    trackId === state.currentTrack?.id
  ) {
    return;
  }

  state.prefetchRequested.add(trackId);

  void fetch(
    "/api/prefetch/"
      + encodeURIComponent(trackId)
      + (intent ? "?intent=1" : ""),
    {
      method: "POST",
      keepalive: true
    }
  ).then(async response => {
    if (!response.ok && response.status !== 202) {
      state.prefetchRequested.delete(trackId);
      return;
    }

    try {
      const payload = await response.clone().json();
      if (payload?.status === "busy") {
        state.prefetchRequested.delete(trackId);
      }
    } catch (_) {}
  }).catch(() => {
    state.prefetchRequested.delete(trackId);
  });

  setTimeout(
    () => state.prefetchRequested.delete(trackId),
    45000
  );
}


function cancelVisiblePrefetchTimers() {
  state.visiblePrefetchGeneration += 1;

  for (const timer of state.visiblePrefetchTimers) {
    clearTimeout(timer);
  }

  state.visiblePrefetchTimers = [];
}


function schedulePrefetchTrack(track, delayMs, generation) {
  if (!track?.id) {
    return;
  }

  const timer = setTimeout(
    () => {
      if (generation !== state.visiblePrefetchGeneration) {
        return;
      }

      if (audio.paused && !state.currentTrack) {
        prefetchTrack(track, false);
      }
    },
    Math.max(0, Number(delayMs || 0))
  );

  state.visiblePrefetchTimers.push(timer);
}


function scheduleVisiblePrefetches(tracks) {
  // V24 deliberately does not guess a track just because it is first in a
  // list. On the small Render instance, a wrong speculative extraction can
  // make the track the user actually taps slower. Explicit hover/focus/touch
  // intent and next-track prefetching still warm useful tracks.
  cancelVisiblePrefetchTimers();
}


function prefetchNextTrack() {
  const next = state.queue.find(
    track => track?.id && track.id !== state.currentTrack?.id
  );

  if (next) {
    prefetchTrack(next, true);
  }
}


async function appendRadioQueue(seedTrackId) {
  const token = ++state.queueRefreshToken;

  try {
    const tracks = await fetchJson(
      "/api/radio/" + encodeURIComponent(seedTrackId)
    );

    if (token !== state.queueRefreshToken || !Array.isArray(tracks)) {
      return;
    }

    const seen = new Set([
      state.currentTrack?.id,
      ...state.queue.map(track => track.id),
      ...state.history.slice(-12).map(track => track.id)
    ].filter(Boolean));

    for (const track of tracks) {
      if (track?.id && !seen.has(track.id)) {
        state.queue.push(track);
        seen.add(track.id);
      }
    }

    if (!audio.paused) {
      prefetchNextTrack();
    }
  } catch (error) {
    console.error("Queue refresh failed:", error);
  }
}


function refreshQueueFromSeed(seedTrackId) {
  state.queue = [];
  void appendRadioQueue(seedTrackId);
}


async function ensureQueue() {
  if (state.queue.length) {
    return;
  }

  if (state.currentTrack?.id) {
    await appendRadioQueue(state.currentTrack.id);
  }

  if (!state.queue.length) {
    const tracks = await fetchJson("/api/recommendations").catch(() => []);
    state.queue = (tracks || []).filter(
      track => track?.id && track.id !== state.currentTrack?.id
    );
  }
}


async function playNext(suppressTransitionSignal) {
  await ensureQueue();

  const next = state.queue.shift();
  if (!next) {
    showToast("NO NEXT TRACK YET");
    return;
  }

  await playTrack(
    next,
    {
      refreshQueue: false,
      suppressTransitionSignal: !!suppressTransitionSignal
    }
  );

  if (state.queue.length < 6) {
    void appendRadioQueue(next.id);
  }
}


async function playPrevious() {
  const previous = state.history.pop();

  if (!previous) {
    if (audio.currentTime > 3) {
      audio.currentTime = 0;
      return;
    }
    showToast("NO PREVIOUS TRACK");
    return;
  }

  if (state.currentTrack) {
    state.queue.unshift(state.currentTrack);
  }

  await playTrack(
    previous,
    {
      refreshQueue: false,
      suppressTransitionSignal: true
    }
  );
}


async function openPlaylistModal(createOnly) {
  const modal = document.getElementById("playlistModal");
  modal.classList.remove("hidden");

  const choices = document.getElementById("playlistChoices");
  choices.innerHTML = '<div class="empty">LOADING PLAYLISTS...</div>';

  const playlists = await fetchJson("/api/playlists").catch(() => []);
  state.playlists = playlists;
  choices.innerHTML = "";

  if (createOnly || !state.currentTrack) {
    choices.innerHTML = '<div class="empty">Create a playlist below.</div>';
    return;
  }

  if (!playlists.length) {
    choices.innerHTML = '<div class="empty">No playlists yet. Create one below.</div>';
    return;
  }

  playlists.forEach(playlist => {
    const button = document.createElement("button");
    button.className = "playlist-choice";
    button.innerHTML = \`<span>\${escapeHtml(playlist.name || "Playlist")}</span><i class="ph ph-plus"></i>\`;
    button.onclick = async () => {
      await addCurrentTrackToPlaylist(playlist.id);
      closePlaylistModal();
    };
    choices.appendChild(button);
  });
}


function closePlaylistModal() {
  document.getElementById("playlistModal").classList.add("hidden");
  document.getElementById("newPlaylistName").value = "";
}


async function addCurrentTrackToPlaylist(playlistId) {
  if (!state.currentTrack) {
    return;
  }

  await fetchJson(
    "/api/playlists/" + encodeURIComponent(playlistId) + "/tracks",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.currentTrack)
    }
  );

  showToast("ADDED TO PLAYLIST");
}


document.getElementById("playlistButton").onclick = () => openPlaylistModal(false);
document.getElementById("playlistModalClose").onclick = closePlaylistModal;
document.getElementById("playlistModal").onclick = event => {
  if (event.target.id === "playlistModal") {
    closePlaylistModal();
  }
};

document.getElementById("newPlaylistCreate").onclick = async () => {
  const name = document.getElementById("newPlaylistName").value.trim();
  if (!name) {
    return;
  }

  const playlist = await fetchJson(
    "/api/playlists",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name })
    }
  );

  if (state.currentTrack && playlist?.id) {
    await addCurrentTrackToPlaylist(playlist.id);
  } else {
    showToast("PLAYLIST CREATED");
  }

  closePlaylistModal();
  state.playlists.unshift(playlist);
};


audio.addEventListener(
  "play",
  () => {

    document.getElementById(
      "playButton"
    ).innerHTML =
      '<i class="ph ph-pause"></i>';

    document.getElementById(
      "expandedPlayButton"
    ).innerHTML =
      '<i class="ph ph-pause"></i>';

    if (state.currentTrack) {
      document.getElementById("nowArtist").textContent =
        state.currentTrack.artist || "";
    }

    if ("mediaSession" in navigator) {
      navigator.mediaSession.playbackState = "playing";
    }

    prefetchNextTrack();

  }
);


audio.addEventListener(
  "pause",
  () => {

    document.getElementById(
      "playButton"
    ).innerHTML =
      '<i class="ph ph-play"></i>';

    document.getElementById(
      "expandedPlayButton"
    ).innerHTML =
      '<i class="ph ph-play"></i>';

    if ("mediaSession" in navigator) {
      navigator.mediaSession.playbackState = "paused";
    }

  }
);


audio.addEventListener(
  "waiting",
  () => {
    if (state.currentTrack) {
      document.getElementById(
        "nowArtist"
      ).textContent =
        (state.currentTrack.artist || "") + " · BUFFERING";
    }
  }
);


audio.addEventListener(
  "stalled",
  () => {
    if (state.currentTrack) {
      document.getElementById(
        "nowArtist"
      ).textContent =
        (state.currentTrack.artist || "") + " · BUFFERING";
    }
  }
);


audio.addEventListener(
  "canplay",
  () => {
    if (!audio.paused) {
      document.getElementById(
        "playButton"
      ).innerHTML =
        '<i class="ph ph-pause"></i>';
    }
  }
);


audio.addEventListener(
  "error",
  () => {
    const mediaError =
      audio.error;

    const currentSrc =
      audio.currentSrc || "";

    console.error(
      "Veeb media element error:",
      mediaError?.code,
      mediaError?.message,
      currentSrc
    );

    if (
      currentSrc &&
      state.expectedAudioSrc &&
      currentSrc !== state.expectedAudioSrc
    ) {
      return;
    }

    if (
      state.playbackPendingGeneration === state.playbackGeneration
    ) {
      return;
    }

    // Mid-song network/media errors get one quiet recovery attempt. The
    // canonical Cloudflare/Render cache usually makes this second request cheap.
    if (
      state.currentTrack &&
      state.mediaRecoveryGeneration !== state.playbackGeneration
    ) {
      state.mediaRecoveryGeneration =
        state.playbackGeneration;

      const resumeAt =
        Number.isFinite(audio.currentTime)
          ? audio.currentTime
          : 0;

      const recoverySrc =
        "/api/audio/"
        + encodeURIComponent(
          state.currentTrack.id
        )
        + "?transport=v25&generation="
        + state.playbackGeneration
        + "&recover=1";

      state.expectedAudioSrc =
        new URL(
          recoverySrc,
          window.location.href
        ).href;

      document.getElementById(
        "playButton"
      ).innerHTML =
        '<i class="ph ph-spinner-gap veeb-spin"></i>';

      window.setTimeout(
        async () => {
          try {
            audio.src =
              recoverySrc;
            audio.load();
            await audio.play();
            if (resumeAt > 0) {
              try {
                audio.currentTime =
                  resumeAt;
              } catch (_) {}
            }
          } catch (error) {
            console.error(
              "Veeb media recovery failed:",
              error
            );
          }
        },
        450
      );

      return;
    }

    document.getElementById(
      "playButton"
    ).innerHTML =
      '<i class="ph ph-play"></i>';

    document.getElementById(
      "expandedPlayButton"
    ).innerHTML =
      '<i class="ph ph-play"></i>';
  }
);



audio.addEventListener(
  "ended",
  async () => {

    await sendListeningEvent(
      "complete"
    );


    await animatePlayerNavigation(
      "next",
      true
    );

  }
);


audio.addEventListener(
  "timeupdate",
  () => {

    document.getElementById(
      "currentTime"
    ).textContent =
      formatTime(
        audio.currentTime
      );


    document.getElementById(
      "totalTime"
    ).textContent =
      formatTime(
        audio.duration
      );


    const seek =
      document.getElementById(
        "seek"
      );


    seek.max =
      audio.duration || 0;


    seek.value =
      audio.currentTime || 0;


    const expandedSeek =
      document.getElementById(
        "expandedSeek"
      );

    expandedSeek.max =
      audio.duration || 0;

    expandedSeek.value =
      audio.currentTime || 0;

    document.getElementById(
      "expandedCurrentTime"
    ).textContent =
      formatTime(
        audio.currentTime
      );

    document.getElementById(
      "expandedTotalTime"
    ).textContent =
      formatTime(
        audio.duration
      );


    if (
      "mediaSession" in navigator &&
      Number.isFinite(audio.duration) &&
      audio.duration > 0
    ) {
      try {
        navigator.mediaSession.setPositionState({
          duration: audio.duration,
          playbackRate: audio.playbackRate || 1,
          position: Math.min(audio.currentTime || 0, audio.duration)
        });
      } catch (_) {}
    }

  }
);


document.getElementById(
  "seek"
).oninput =
  event => {

    audio.currentTime =
      Number(
        event.target.value
      );

  };


async function sendListeningEvent(
  type
) {

  if (
    !state.currentTrack
  ) {
    return;
  }


  await fetch(
    "/api/events",
    {
      method:
        "POST",

      headers: {
        "Content-Type":
          "application/json"
      },

      body:
        JSON.stringify({
          trackId:
            state.currentTrack.id,

          type,

          positionSeconds:
            audio.currentTime || 0,

          durationSeconds:
            audio.duration || 0
        })
    }
  );

}


function setMediaSession(
  track
) {

  if (
    !(
      "mediaSession"
      in navigator
    )
  ) {
    return;
  }


  navigator.mediaSession.metadata =
    new MediaMetadata({

      title:
        track.title || "",

      artist:
        track.artist || "",

      album:
        track.album || "",

      artwork:
        track.artwork
          ? [
              {
                src:
                  track.artwork
              }
            ]
          : []

    });


  navigator.mediaSession.setActionHandler(
    "play",
    () => audio.play()
  );


  navigator.mediaSession.setActionHandler(
    "pause",
    () => audio.pause()
  );


  navigator.mediaSession.setActionHandler(
    "nexttrack",
    () => animatePlayerNavigation("next", false)
  );


  navigator.mediaSession.setActionHandler(
    "previoustrack",
    () => animatePlayerNavigation("previous", true)
  );


  try {
    navigator.mediaSession.setActionHandler(
      "seekto",
      details => {
        if (Number.isFinite(details.seekTime)) {
          audio.currentTime = details.seekTime;
        }
      }
    );
  } catch (_) {}

}


function setSection(
  value
) {

  document.getElementById(
    "sectionLabel"
  ).textContent =
    value;

}


function setResultsMessage(
  value
) {

  const container = document.getElementById(
    "results"
  );

  container.className = "";

  container.innerHTML =
    '<div class="empty">'
    + escapeHtml(value)
    + '</div>';

}


function formatTime(
  seconds
) {

  if (
    !Number.isFinite(
      seconds
    )
  ) {
    return "0:00";
  }


  const minutes =
    Math.floor(
      seconds / 60
    );


  const remaining =
    Math.floor(
      seconds % 60
    );


  return (
    minutes
    + ":"
    + String(
        remaining
      ).padStart(
        2,
        "0"
      )
  );

}


function showToast(
  text
) {

  const toast =
    document.getElementById(
      "toast"
    );


  toast.textContent =
    text;


  toast.style.display =
    "block";


  clearTimeout(
    window.toastTimer
  );


  window.toastTimer =
    setTimeout(
      () => {

        toast.style.display =
          "none";

      },
      1700
    );

}


function escapeHtml(
  value
) {

  return String(
    value
  )

    .replaceAll(
      "&",
      "&amp;"
    )

    .replaceAll(
      "<",
      "&lt;"
    )

    .replaceAll(
      ">",
      "&gt;"
    )

    .replaceAll(
      '"',
      "&quot;"
    )

    .replaceAll(
      "'",
      "&#039;"
    );

}


initialise();

</script>

</body>

</html>
`;
