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
            count(*) filter (
                where trade_count > 0 and session_status <> 'running'
            )::integer as session_count,
            count(*) filter (
                where trade_count > 0
                  and session_status <> 'running'
                  and net_profit > 0
            )::integer as wins,
            count(*) filter (
                where trade_count > 0
                  and session_status <> 'running'
                  and net_profit <= 0
            )::integer as losses,
            coalesce(
                round(
                    100.0 * count(*) filter (
                        where trade_count > 0
                          and session_status <> 'running'
                          and net_profit > 0
                    )
                    / nullif(count(*) filter (
                        where trade_count > 0 and session_status <> 'running'
                    ), 0),
                    1
                ),
                0
            ) as win_rate,
            coalesce(sum(net_profit) filter (
                where trade_count > 0 and session_status <> 'running'
            ), 0)::numeric(18, 2) as net_pnl,
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
          and session_status <> 'running'
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