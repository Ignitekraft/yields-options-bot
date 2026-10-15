create table if not exists public.trade_history (
    id bigint generated always as identity primary key,
    telegram_user_id text not null,
    contract_id text not null,
    account_mode text not null check (account_mode in ('demo', 'real')),
    market_symbol text not null,
    direction text not null check (direction in ('BULLISH', 'BEARISH')),
    stake numeric(18, 2) not null check (stake > 0),
    profit numeric(18, 2) not null,
    outcome text not null check (outcome in ('win', 'loss')),
    direction_accurate boolean,
    tokens_spent integer not null default 0 check (tokens_spent >= 0),
    settled_at timestamptz not null default now(),
    unique (telegram_user_id, contract_id)
);

create index if not exists trade_history_user_settled_idx
    on public.trade_history (telegram_user_id, settled_at desc);

create index if not exists trade_history_user_mode_idx
    on public.trade_history (telegram_user_id, account_mode, settled_at desc);

alter table public.trade_history enable row level security;
revoke all on public.trade_history from anon, authenticated;
grant all on public.trade_history to service_role;
grant usage, select on sequence public.trade_history_id_seq to service_role;

create or replace function public.get_user_trade_stats(p_telegram_user_id text)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
    with mode_summary as (
        select
            account_mode,
            count(*)::integer as trade_count,
            count(*) filter (where outcome = 'win')::integer as wins,
            count(*) filter (where outcome = 'loss')::integer as losses,
            coalesce(round(100.0 * count(*) filter (where outcome = 'win') / nullif(count(*), 0), 1), 0) as win_rate,
            coalesce(
                round(
                    100.0 * count(*) filter (where direction_accurate is true)
                    / nullif(count(*) filter (where direction_accurate is not null), 0),
                    1
                ),
                0
            ) as accuracy,
            coalesce(sum(profit), 0)::numeric(18, 2) as net_pnl,
            coalesce(sum(tokens_spent), 0)::integer as tokens_spent
        from public.trade_history
        where telegram_user_id = p_telegram_user_id
        group by account_mode
    ),
    recent_trades as (
        select
            contract_id,
            account_mode,
            market_symbol,
            direction,
            stake,
            profit,
            outcome,
            direction_accurate,
            settled_at
        from public.trade_history
        where telegram_user_id = p_telegram_user_id
        order by settled_at desc
        limit 10
    )
    select jsonb_build_object(
        'recent', coalesce((select jsonb_agg(to_jsonb(recent_trades) order by settled_at desc) from recent_trades), '[]'::jsonb),
        'real', coalesce(
            (select to_jsonb(mode_summary) - 'account_mode' from mode_summary where account_mode = 'real'),
            '{"trade_count":0,"wins":0,"losses":0,"win_rate":0,"accuracy":0,"net_pnl":0,"tokens_spent":0}'::jsonb
        ),
        'demo', coalesce(
            (select to_jsonb(mode_summary) - 'account_mode' from mode_summary where account_mode = 'demo'),
            '{"trade_count":0,"wins":0,"losses":0,"win_rate":0,"accuracy":0,"net_pnl":0,"tokens_spent":0}'::jsonb
        )
    );
$$;

revoke all on function public.get_user_trade_stats(text) from public, anon, authenticated;
grant execute on function public.get_user_trade_stats(text) to service_role;