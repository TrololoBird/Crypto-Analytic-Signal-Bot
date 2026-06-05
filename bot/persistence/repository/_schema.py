"""Repository DDL constants (extracted from memory.py)."""

from __future__ import annotations

REPOSITORY_CORE_DDL = """
            -- LEGACY TABLE — read-only. Writes removed in Phase E.
            -- Retained for dashboard backward-compatibility. Drop in a future migration.
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit_1 REAL NOT NULL,
                take_profit_2 REAL NOT NULL,
                score REAL NOT NULL,
                created_at TEXT NOT NULL,
                timeframe TEXT DEFAULT '1h',
                atr_pct REAL DEFAULT 0.0,
                spread_bps REAL DEFAULT 0.0,
                rsi_1h REAL,
                adx_1h REAL,
                volume_ratio REAL,
                funding_rate REAL,
                oi_change_pct REAL,
                features TEXT,  -- JSON
                metadata TEXT,  -- JSON
                outcome_id TEXT  -- Reference to outcome
            );

            CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
            CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id);
            CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

            -- LEGACY TABLE — read-only. Writes removed in Phase E.
            CREATE TABLE IF NOT EXISTS outcomes (
                outcome_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price_1h REAL,
                price_4h REAL,
                price_24h REAL,
                pnl_1h REAL,
                pnl_4h REAL,
                pnl_24h REAL,
                max_profit_pct REAL DEFAULT 0.0,
                max_loss_pct REAL DEFAULT 0.0,
                mae REAL DEFAULT 0.0,
                mfe REAL DEFAULT 0.0,
                hit_tp1 INTEGER DEFAULT 0,
                hit_tp2 INTEGER DEFAULT 0,
                hit_sl INTEGER DEFAULT 0,
                result TEXT DEFAULT '',
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                time_to_tp1_min INTEGER,
                time_to_tp2_min INTEGER,
                time_to_sl_min INTEGER,
                FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
            );

            CREATE INDEX IF NOT EXISTS idx_outcomes_symbol ON outcomes(symbol);
            CREATE INDEX IF NOT EXISTS idx_outcomes_signal ON outcomes(signal_id);
            CREATE INDEX IF NOT EXISTS idx_outcomes_result ON outcomes(result);

            CREATE TABLE IF NOT EXISTS config_versions (
                version_id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 0
            );

            -- Cooldown tracking (replaces SignalCooldownStore JSON)
            CREATE TABLE IF NOT EXISTS cooldowns (
                cooldown_key TEXT PRIMARY KEY,
                last_sent_at TEXT NOT NULL,
                setup_id TEXT,
                symbol TEXT,
                cooldown_type TEXT DEFAULT 'signal_key'  -- 'signal_key' or 'symbol'
            );

            CREATE INDEX IF NOT EXISTS idx_cooldowns_symbol ON cooldowns(symbol);
            CREATE INDEX IF NOT EXISTS idx_cooldowns_setup ON cooldowns(setup_id);

            -- Setup adaptive scoring (replaces setup_score_adjustments JSON)
            CREATE TABLE IF NOT EXISTS setup_scores (
                setup_id TEXT PRIMARY KEY,
                score_adjustment REAL DEFAULT 0.0,
                outcome_window TEXT,  -- JSON array of last 20 outcomes
                updated_at TEXT NOT NULL
            );

            -- Active signal tracking (replaces SignalTrackingStore JSON)
            CREATE TABLE IF NOT EXISTS active_signals (
                tracking_id TEXT PRIMARY KEY,
                tracking_ref TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                setup_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                timeframe TEXT,
                created_at TEXT NOT NULL,
                pending_expires_at TEXT,
                active_expires_at TEXT,
                entry_low REAL,
                entry_high REAL,
                entry_mid REAL,
                initial_stop REAL,
                stop REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                take_profit_3 REAL,
                valid_until TEXT,
                scale_weights TEXT,
                ttl_bars INTEGER,
                single_target_mode INTEGER DEFAULT 0,
                target_integrity_status TEXT DEFAULT 'unchecked',
                score REAL,
                risk_reward REAL,
                reasons TEXT,  -- JSON array
                signal_message_id INTEGER,
                bias_4h TEXT DEFAULT 'neutral',
                quote_volume REAL,
                spread_bps REAL,
                atr_pct REAL,
                orderflow_delta_ratio REAL,
                status TEXT DEFAULT 'pending',  -- pending, active, closed
                activated_at TEXT,
                activation_price REAL,
                tp1_hit_at TEXT,
                tp2_hit_at TEXT,
                stop_price REAL,
                tp1_price REAL,
                tp2_price REAL,
                tp3_price REAL,
                last_checked_at TEXT,
                last_price REAL,
                closed_at TEXT,
                close_reason TEXT,
                close_price REAL
            );

            CREATE INDEX IF NOT EXISTS idx_active_signals_symbol ON active_signals(symbol);
            CREATE INDEX IF NOT EXISTS idx_active_signals_status ON active_signals(status);
            CREATE INDEX IF NOT EXISTS idx_active_signals_status_symbol
                ON active_signals(status, symbol);
            CREATE INDEX IF NOT EXISTS idx_active_signals_setup ON active_signals(setup_id);
            CREATE INDEX IF NOT EXISTS idx_active_signals_created ON active_signals(created_at);

            CREATE TABLE IF NOT EXISTS tracking_stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                signals_sent INTEGER DEFAULT 0,
                activated INTEGER DEFAULT 0,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                stop_loss INTEGER DEFAULT 0,
                expired INTEGER DEFAULT 0,
                ambiguous_exit INTEGER DEFAULT 0
            );

            INSERT OR IGNORE INTO tracking_stats (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS signal_outcomes (
                tracking_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                tracking_ref TEXT NOT NULL,
                symbol TEXT NOT NULL,
                setup_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                created_at TEXT NOT NULL,
                activated_at TEXT,
                closed_at TEXT,
                entry_price REAL,
                exit_price REAL,
                result TEXT NOT NULL,
                pnl_pct REAL DEFAULT 0.0,
                pnl_r_multiple REAL DEFAULT 0.0,
                max_profit_pct REAL DEFAULT 0.0,
                max_loss_pct REAL DEFAULT 0.0,
                mae REAL DEFAULT 0.0,
                mfe REAL DEFAULT 0.0,
                time_to_entry_min INTEGER DEFAULT 0,
                time_to_exit_min INTEGER DEFAULT 0,
                features TEXT,
                was_profitable INTEGER DEFAULT 0,
                llm_was_correct INTEGER,
                setup_quality TEXT DEFAULT 'neutral'
            );

            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_symbol ON signal_outcomes(symbol);
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_setup ON signal_outcomes(setup_id);
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_result ON signal_outcomes(result);
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_closed_at ON signal_outcomes(closed_at);
        """
