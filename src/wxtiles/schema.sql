-- Run state for the ingest pipeline. Object storage holds the data (COGs, manifests); this holds
-- what is expected, what arrived, from where, what QC said, and the job queue.

create table if not exists clock (
    id int primary key default 1 check (id = 1),
    sim_now timestamptz not null
);

create table if not exists runs (
    source text not null,
    run text not null,
    run_time timestamptz not null,
    state text not null default 'in_progress',   -- in_progress | complete | held
    steps_expected int not null,
    held_reason text,
    revision int not null default 1,
    created_at timestamptz not null default now(),
    finalized_at timestamptz,
    promoted_at timestamptz,
    primary key (source, run)
);

create table if not exists steps (
    source text not null,
    run text not null,
    step text not null,                 -- f006, m03/f006
    fhour int not null,
    member int,
    state text not null default 'expected',   -- expected | done | missing | qc_failed
    expected_at timestamptz not null,   -- sim clock
    deadline timestamptz not null,      -- sim clock
    mirror text,
    attempts int not null default 0,
    bytes_fetched bigint,
    bytes_full bigint,
    posted_wall timestamptz,            -- when the object appeared upstream (wall clock)
    done_wall timestamptz,              -- when its fields were published (wall clock)
    ingest_ms int,
    last_error text,
    primary key (source, run, step)
);
create index if not exists steps_open on steps (source, run) where state = 'expected';

create table if not exists products (
    source text not null,
    run text not null,
    field text not null,
    fhour int not null,
    cog_key text not null,
    zoom int not null,
    vmin real,
    vmax real,
    published_wall timestamptz not null default now(),
    primary key (source, run, field, fhour)
);

create table if not exists qc_results (
    id bigserial primary key,
    source text not null,
    run text not null,
    step text not null,
    field text not null,
    check_name text not null,
    passed boolean not null,
    value double precision,
    detail text,
    at_wall timestamptz not null default now()
);
create index if not exists qc_failed on qc_results (source, run) where not passed;

create table if not exists jobs (
    id bigserial primary key,
    kind text not null,                 -- ingest | derive | finalize | ai_run | warm
    dedupe text not null,
    payload jsonb not null,
    priority int not null default 5,    -- lower runs first
    state text not null default 'queued',   -- queued | running | done | failed
    run_after timestamptz not null default now(),
    attempts int not null default 0,
    lease_until timestamptz,
    worker text,
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists jobs_dedupe_open on jobs (dedupe) where state in ('queued', 'running');
create index if not exists jobs_ready on jobs (priority, run_after) where state = 'queued';

-- New-object notifications: stands in for NOAA's SNS topics delivered to an SQS queue.
create table if not exists upstream_events (
    id bigserial primary key,
    mirror text not null,
    key text not null,
    posted_wall timestamptz not null default now(),
    consumed boolean not null default false
);
create index if not exists upstream_open on upstream_events (id) where not consumed;

create table if not exists events (
    id bigserial primary key,
    at_sim timestamptz,
    at_wall timestamptz not null default now(),
    source text,
    run text,
    kind text not null,                 -- failover | integrity | missing | qc_failed | held | promoted | late | lease_expired | retry
    detail text
);
create index if not exists events_recent on events (id desc);
