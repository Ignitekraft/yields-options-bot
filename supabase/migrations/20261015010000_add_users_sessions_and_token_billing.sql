create table if not exists public.users (
    telegram_user_id text primary key,
    telegram_first_name text,
    telegram_last_name text,
    telegram_username text,
    current_account_mode text not null default 'demo'
        check (current_account_mode in ('demo', 'real')),
    demo_balance numeric(18, 2),
    demo_currency text,
    demo_balance_updated_at timestamptz,
    real_balance numeric(18, 2),
    real_currency text,
    real_balance_updated_at timestamptz,
    token_balance integer not null default 50 check (token_balance >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.auto_trade_sessions (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id text not null references public.users(telegram_user_id),
    account_mode text not null check (account_mode in ('demo', 'real')),
    market_symbol text not null,
    direction text not null check (direction in ('BULLISH', 'BEARISH')),
    initial_stake numeric(18, 2) not null check (initial_stake > 0),
    target_winning_cycles integer not null check (target_winning_cycles > 0),
    session_status text not null default 'running'
        check (session_status in ('running', 'completed', 'cancelled', 'stopped', 'failed')),
    winning_cycles integer not null default 0,
    trade_count integer not null default 0,
    total_staked numeric(18, 2) not null default 0,
    net_profit numeric(18, 2) not null default 0,
    tokens_spent integer not null default 0 check (tokens_spent >= 0),
    started_at timestamptz not null default now(),
    completed_at timestamptz
);

create index if not exists auto_trade_sessions_user_started_idx
    on public.auto_trade_sessions (telegram_user_id, started_at desc);
create index if not exists auto_trade_sessions_user_mode_started_idx
    on public.auto_trade_sessions (telegram_user_id, account_mode, started_at desc);

alter table public.trade_history
    add column if not exists session_id uuid references public.auto_trade_sessions(id) on delete set null;
alter table public.trade_history
    add column if not exists tokens_spent integer not null default 1 check (tokens_spent >= 0);
create index if not exists trade_history_session_idx
    on public.trade_history (session_id, settled_at desc);

alter table public.users enable row level security;
alter table public.auto_trade_sessions enable row level security;
revoke all on public.users, public.auto_trade_sessions from anon, authenticated;
grant all on public.users, public.auto_trade_sessions to service_role;

create or replace function public.reserve_auto_trade_token(
    p_telegram_user_id text,
    p_session_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    remaining_tokens integer;
begin
    update public.users
    set token_balance = token_balance - 1,
        updated_at = now()
    where telegram_user_id = p_telegram_user_id
      and token_balance > 0
    returning token_balance into remaining_tokens;

    if not found then
        select token_balance into remaining_tokens
        from public.users
        where telegram_user_id = p_telegram_user_id;

        return jsonb_build_object(
            'ok', false,
            'reason', 'INSUFFICIENT_TOKENS',
            'token_balance', coalesce(remaining_tokens, 0)
        );
    end if;

    update public.auto_trade_sessions
    set tokens_spent = tokens_spent + 1
    where id = p_session_id
      and telegram_user_id = p_telegram_user_id
      and session_status = 'running';

    if not found then
        raise exception 'AUTO_TRADE_SESSION_NOT_FOUND';
    end if;

    return jsonb_build_object('ok', true, 'token_balance', remaining_tokens);
end;
$$;

create or replace function public.refund_auto_trade_token(
    p_telegram_user_id text,
    p_session_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    remaining_tokens integer;
begin
    update public.users
    set token_balance = token_balance + 1,
        updated_at = now()
    where telegram_user_id = p_telegram_user_id
    returning token_balance into remaining_tokens;

    if not found then
        raise exception 'TRADE_USER_NOT_FOUND';
    end if;

    update public.auto_trade_sessions
    set tokens_spent = greatest(tokens_spent - 1, 0)
    where id = p_session_id
      and telegram_user_id = p_telegram_user_id;

    if not found then
        raise exception 'AUTO_TRADE_SESSION_NOT_FOUND';
    end if;

    return jsonb_build_object('ok', true, 'token_balance', remaining_tokens);
end;
$$;

revoke all on function public.reserve_auto_trade_token(text, uuid) from public, anon, authenticated;
revoke all on function public.refund_auto_trade_token(text, uuid) from public, anon, authenticated;
grant execute on function public.reserve_auto_trade_token(text, uuid) to service_role;
grant execute on function public.refund_auto_trade_token(text, uuid) to service_role;

create or replace function public.get_user_trade_stats(p_telegram_user_id text)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
    with session_summary as (
        select
            account_mode,
            count(*) filter (where trade_count > 0)::integer as session_count,
            count(*) filter (where trade_count > 0 and net_profit > 0)::integer as wins,
            count(*) filter (where trade_count > 0 and net_profit <= 0)::integer as losses,
            coalesce(
                round(
                    100.0 * count(*) filter (where trade_count > 0 and net_profit > 0)
                    / nullif(count(*) filter (where trade_count > 0), 0),
                    1
                ),
                0
            ) as win_rate,
            coalesce(sum(net_profit) filter (where trade_count > 0), 0)::numeric(18, 2) as net_pnl,
            coalesce(sum(tokens_spent), 0)::integer as tokens_spent
        from public.auto_trade_sessions
        where telegram_user_id = p_telegram_user_id
        group by account_mode
    ),
    trade_accuracy as (
        select
            account_mode,
            coalesce(
                round(
                    100.0 * count(*) filter (where direction_accurate is true)
                    / nullif(count(*) filter (where direction_accurate is not null), 0),
                    1
                ),
                0
            ) as accuracy
        from public.trade_history
        where telegram_user_id = p_telegram_user_id
        group by account_mode
    ),
    recent_sessions as (
        select
            id as session_id,
            account_mode,
            market_symbol,
            direction,
            net_profit,
            session_status,
            trade_count,
            winning_cycles,
            tokens_spent,
            started_at,
            completed_at
        from public.auto_trade_sessions
        where telegram_user_id = p_telegram_user_id
          and trade_count > 0
        order by coalesce(completed_at, started_at) desc
        limit 10
    )
    select jsonb_build_object(
        'token_balance', coalesce(
            (select token_balance from public.users where telegram_user_id = p_telegram_user_id),
            0
        ),
        'recent', coalesce(
            (select jsonb_agg(to_jsonb(recent_sessions) order by coalesce(completed_at, started_at) desc) from recent_sessions),
            '[]'::jsonb
        ),
        'real', coalesce(
            (
                select jsonb_build_object(
                    'session_count', session_summary.session_count,
                    'wins', session_summary.wins,
                    'losses', session_summary.losses,
                    'win_rate', session_summary.win_rate,
                    'accuracy', coalesce(trade_accuracy.accuracy, 0),
                    'net_pnl', session_summary.net_pnl,
                    'tokens_spent', session_summary.tokens_spent
                )
                from session_summary
                left join trade_accuracy using (account_mode)
                where session_summary.account_mode = 'real'
            ),
            '{"session_count":0,"wins":0,"losses":0,"win_rate":0,"accuracy":0,"net_pnl":0,"tokens_spent":0}'::jsonb
        ),
        'demo', coalesce(
            (
                select jsonb_build_object(
                    'session_count', session_summary.session_count,
                    'wins', session_summary.wins,
                    'losses', session_summary.losses,
                    'win_rate', session_summary.win_rate,
                    'accuracy', coalesce(trade_accuracy.accuracy, 0),
                    'net_pnl', session_summary.net_pnl,
                    'tokens_spent', session_summary.tokens_spent
                )
                from session_summary
                left join trade_accuracy using (account_mode)
                where session_summary.account_mode = 'demo'
            ),
            '{"session_count":0,"wins":0,"losses":0,"win_rate":0,"accuracy":0,"net_pnl":0,"tokens_spent":0}'::jsonb
        )
    );
$$;

revoke all on function public.get_user_trade_stats(text) from public, anon, authenticated;
grant execute on function public.get_user_trade_stats(text) to service_role;