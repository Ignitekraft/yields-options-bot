alter table public.users
    add column if not exists smart_scaling_enabled boolean not null default true;
