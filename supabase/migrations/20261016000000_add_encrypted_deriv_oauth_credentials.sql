create table if not exists public.deriv_oauth_credentials (
    telegram_user_id text primary key
        references public.users(telegram_user_id) on delete cascade,
    encrypted_credentials text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.deriv_oauth_credentials enable row level security;
revoke all on public.deriv_oauth_credentials from anon, authenticated;
grant all on public.deriv_oauth_credentials to service_role;
