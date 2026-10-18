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
    latest_session timestamptz;
    latest_market_symbol text;
    retry_after_seconds integer;
    v_now timestamptz;
begin
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'auto_trade_session:' || p_telegram_user_id,
            7419283104521::bigint
        )
    );

    v_now := clock_timestamp();
    select count(*)::integer, max(started_at)
    into sessions_started, latest_session
    from public.auto_trade_sessions
    where telegram_user_id = p_telegram_user_id
      and started_at > v_now - interval '15 minutes';

    if sessions_started >= 3 then
        retry_after_seconds := greatest(
            0,
            ceil(
                extract(
                    epoch from latest_session + interval '15 minutes' - v_now
                )
            )::integer
        );
        return jsonb_build_object(
            'started', false,
            'sessions_started', sessions_started,
            'limit', 3,
            'retry_after_seconds', retry_after_seconds
        );
    end if;

    select market_symbol
    into latest_market_symbol
    from public.auto_trade_sessions
    where telegram_user_id = p_telegram_user_id
    order by started_at desc, id desc
    limit 1;

    if latest_market_symbol is not null
       and (case upper(btrim(latest_market_symbol))
           when 'STEP_100' then 'STPRNG'
           when 'STEP_200' then 'STPRNG2'
           when 'STEP_300' then 'STPRNG3'
           when 'STEP_400' then 'STPRNG4'
           when 'STEP_500' then 'STPRNG5'
           else upper(btrim(latest_market_symbol))
       end) = (case upper(btrim(p_market_symbol))
           when 'STEP_100' then 'STPRNG'
           when 'STEP_200' then 'STPRNG2'
           when 'STEP_300' then 'STPRNG3'
           when 'STEP_400' then 'STPRNG4'
           when 'STEP_500' then 'STPRNG5'
           else upper(btrim(p_market_symbol))
       end)
    then
        return jsonb_build_object(
            'started', false,
            'same_market', true,
            'previous_market_symbol', latest_market_symbol,
            'sessions_started', sessions_started,
            'limit', 3,
            'retry_after_seconds', 0
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
        'limit', 3,
        'retry_after_seconds', 0
    );
end;
$$;
