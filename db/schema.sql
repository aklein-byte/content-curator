-- Content Curator Database Schema

-- Candidates: Images found by curator, pending review
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    niche TEXT NOT NULL,
    image_url TEXT NOT NULL UNIQUE,
    source_url TEXT,
    source_name TEXT,
    title TEXT,
    description TEXT,
    curator_notes TEXT,
    quality_score INTEGER CHECK (quality_score >= 1 AND quality_score <= 10),
    found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_at TIMESTAMP,
    rejection_reason TEXT
);

-- Approved: Content ready to post
CREATE TABLE IF NOT EXISTS approved (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    niche TEXT NOT NULL,
    candidate_id INTEGER REFERENCES candidates(id),
    image_url TEXT NOT NULL,
    caption TEXT,
    hashtags TEXT,
    scheduled_for TIMESTAMP,
    approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'posted', 'failed'))
);

-- Posted: Content history and engagement tracking
CREATE TABLE IF NOT EXISTS posted (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    niche TEXT NOT NULL,
    approved_id INTEGER REFERENCES approved(id),
    platform TEXT NOT NULL,  -- 'x', 'instagram', etc.
    post_id TEXT,  -- Platform's post ID
    image_url TEXT NOT NULL,
    caption TEXT,
    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Engagement metrics (updated periodically)
    likes INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    impressions INTEGER DEFAULT 0,
    engagement_updated_at TIMESTAMP
);

-- Sources: Track which sources we've scraped
CREATE TABLE IF NOT EXISTS source_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    source_name TEXT,
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    images_found INTEGER DEFAULT 0,
    status TEXT DEFAULT 'success' CHECK (status IN ('success', 'failed', 'partial'))
);

-- Analytics: Daily metrics
CREATE TABLE IF NOT EXISTS daily_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    niche TEXT NOT NULL,
    platform TEXT NOT NULL,
    date DATE NOT NULL,
    followers INTEGER,
    posts_count INTEGER,
    total_likes INTEGER,
    total_reposts INTEGER,
    total_impressions INTEGER,
    UNIQUE(niche, platform, date)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_niche ON candidates(niche);
CREATE INDEX IF NOT EXISTS idx_approved_status ON approved(status);
CREATE INDEX IF NOT EXISTS idx_posted_platform ON posted(platform);
CREATE INDEX IF NOT EXISTS idx_posted_niche ON posted(niche);
