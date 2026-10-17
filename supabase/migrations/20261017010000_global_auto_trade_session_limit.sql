create index if not exists auto_trade_sessions_started_at_idx
    on public.auto_trade_sessions (started_at);

create or replace function public.get_auto_trade_session_limit_status()
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
    with recent_sessions as (
        select started_at
        from public.auto_trade_sessions
        where started_at > statement_timestamp() - interval '1 hour'
    ),
    session_summary as (
        select
            count(*)::integer as sessions_started,
            min(started_at) as oldest_session
        from recent_sessions
    )
    select jsonb_build_object(
        'sessions_started', sessions_started,
        'limit', 10,
        'locked', sessions_started >= 10,
        'retry_after_seconds',
            case
                when sessions_started < 10 then 0
                else greatest(
                    0,
                    ceil(
                        extract(
                            epoch from oldest_session + interval '1 hour'
                            - statement_timestamp()
                        )
                    )::integer
                )
            end
    )
    from session_summary;
$$;

create or replace function public.start_auto_trade_session(
    p_session_id uuid,
    p_telegram_user_id text,
    p_account_mode text,
    p_market_symbol text,
    p_direction text,
    p_initial_stake numeric,
    p_target_winning_cycles integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    sessions_started integer;
    oldest_session timestamptz;
    retry_after_seconds integer;
    v_now timestamptz;
begin
    perform pg_advisory_xact_lock(7419283104521);

    v_now := clock_timestamp();
    select count(*)::integer, min(started_at)
    into sessions_started, oldest_session
    from public.auto_trade_sessions
    where started_at > v_now - interval '1 hour';

    if sessions_started >= 10 then
        retry_after_seconds := greatest(
            0,
            ceil(
                extract(
                    epoch from oldest_session + interval '1 hour' - v_now
                )
            )::integer
        );
        return jsonb_build_object(
            'started', false,
            'sessions_started', sessions_started,
            'limit', 10,
            'retry_after_seconds', retry_after_seconds
        );
    end if;

    insert into public.auto_trade_sessions (
        id,
        telegram_user_id,
        account_mode,
        market_symbol,
        direction,
        initial_stake,
        target_winning_cycles,
        session_status,
        started_at
    )
    values (
        p_session_id,
        p_telegram_user_id,
        p_account_mode,
        p_market_symbol,
        p_direction,
        round(p_initial_stake, 2),
        p_target_winning_cycles,
        'running',
        v_now
    );

    return jsonb_build_object(
        'started', true,
        'sessions_started', sessions_started + 1,
        'limit', 10,
        'retry_after_seconds', 0
    );
end;
$$;

revoke all on function public.get_auto_trade_session_limit_status()
    from public, anon, authenticated;
revoke all on function public.start_auto_trade_session(
    uuid, text, text, text, text, numeric, integer
) from public, anon, authenticated;
grant execute on function public.get_auto_trade_session_limit_status()
    to service_role;
grant execute on function public.start_auto_trade_session(
    uuid, text, text, text, text, numeric, integer
) to service_role;
