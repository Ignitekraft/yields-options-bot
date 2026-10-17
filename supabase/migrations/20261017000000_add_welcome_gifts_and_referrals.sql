alter table public.users
    add column if not exists welcome_gift_200_claimed boolean not null default false,
    add column if not exists is_admin boolean not null default false,
    add column if not exists referred_by_telegram_user_id text
        references public.users(telegram_user_id),
    add column if not exists referral_balance numeric(18, 2) not null default 0
        check (referral_balance >= 0),
    add column if not exists payout_country text,
    add column if not exists payout_details_encrypted text,
    add column if not exists payout_input_step text
        check (payout_input_step in ('country', 'details'));

create index if not exists users_referrer_idx
    on public.users (referred_by_telegram_user_id);

create table if not exists public.token_purchases (
    id uuid primary key default gen_random_uuid(),
    provider text not null check (provider in ('whop', 'manual')),
    provider_purchase_id text not null unique,
    provider_event_id text unique,
    telegram_user_id text not null references public.users(telegram_user_id),
    tokens integer not null check (tokens > 0),
    amount_paid numeric(18, 2) not null check (amount_paid > 0),
    currency text not null check (currency = 'USD'),
    verified_by_telegram_user_id text references public.users(telegram_user_id),
    created_at timestamptz not null default now()
);

create table if not exists public.referral_commissions (
    id uuid primary key default gen_random_uuid(),
    purchase_id uuid not null unique
        references public.token_purchases(id) on delete restrict,
    referrer_telegram_user_id text not null
        references public.users(telegram_user_id),
    buyer_telegram_user_id text not null
        references public.users(telegram_user_id),
    amount_paid numeric(18, 2) not null check (amount_paid > 0),
    commission_amount numeric(18, 2) not null check (commission_amount >= 0),
    currency text not null check (currency = 'USD'),
    created_at timestamptz not null default now()
);

create index if not exists referral_commissions_referrer_idx
    on public.referral_commissions (referrer_telegram_user_id, created_at desc);

create table if not exists public.referral_withdrawals (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id text not null references public.users(telegram_user_id),
    amount numeric(18, 2) not null check (amount >= 10),
    currency text not null default 'USD' check (currency = 'USD'),
    status text not null default 'pending'
        check (status in ('pending', 'paid', 'failed')),
    requested_at timestamptz not null default now(),
    scheduled_for date not null,
    payout_details_snapshot_encrypted text not null,
    paid_at timestamptz,
    failure_reason text,
    updated_at timestamptz not null default now()
);

create index if not exists referral_withdrawals_due_idx
    on public.referral_withdrawals (scheduled_for, status);
create index if not exists referral_withdrawals_user_idx
    on public.referral_withdrawals (telegram_user_id, requested_at desc);
create unique index if not exists referral_withdrawals_one_pending_per_user_idx
    on public.referral_withdrawals (telegram_user_id)
    where status = 'pending';

alter table public.token_purchases enable row level security;
alter table public.referral_commissions enable row level security;
alter table public.referral_withdrawals enable row level security;
revoke all on public.token_purchases, public.referral_commissions,
    public.referral_withdrawals from anon, authenticated;
grant all on public.token_purchases, public.referral_commissions,
    public.referral_withdrawals to service_role;

create or replace function public.claim_welcome_gift_200(
    p_telegram_user_id text
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
    set token_balance = token_balance + 200,
        welcome_gift_200_claimed = true,
        updated_at = now()
    where telegram_user_id = p_telegram_user_id
      and welcome_gift_200_claimed = false
    returning token_balance into remaining_tokens;

    if found then
        return jsonb_build_object(
            'granted', true,
            'tokens_granted', 200,
            'token_balance', remaining_tokens
        );
    end if;

    select token_balance into remaining_tokens
    from public.users
    where telegram_user_id = p_telegram_user_id;

    if not found then
        raise exception 'TRADE_USER_NOT_FOUND';
    end if;

    return jsonb_build_object(
        'granted', false,
        'tokens_granted', 0,
        'token_balance', remaining_tokens
    );
end;
$$;

create or replace function public.attach_referral(
    p_telegram_user_id text,
    p_referrer_telegram_user_id text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_telegram_user_id = p_referrer_telegram_user_id
       or not exists (
           select 1 from public.users
           where telegram_user_id = p_referrer_telegram_user_id
       ) then
        return false;
    end if;

    update public.users
    set referred_by_telegram_user_id = p_referrer_telegram_user_id,
        updated_at = now()
    where telegram_user_id = p_telegram_user_id
      and referred_by_telegram_user_id is null;

    return found;
end;
$$;

create or replace function public.record_verified_token_purchase(
    p_provider text,
    p_provider_purchase_id text,
    p_provider_event_id text,
    p_telegram_user_id text,
    p_tokens integer,
    p_amount_paid numeric,
    p_currency text,
    p_verified_by_telegram_user_id text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    new_purchase_id uuid;
    referrer_id text;
    new_commission numeric(18, 2) := 0;
    remaining_tokens integer;
begin
    if p_provider not in ('whop', 'manual')
       or p_provider_purchase_id is null
       or length(trim(p_provider_purchase_id)) = 0
       or p_tokens <= 0
       or p_amount_paid <= 0
       or upper(p_currency) <> 'USD' then
        raise exception 'TOKEN_PURCHASE_INVALID';
    end if;

    if p_provider = 'manual' and not exists (
        select 1 from public.users
        where telegram_user_id = p_verified_by_telegram_user_id
          and is_admin = true
    ) then
        raise exception 'REFERRAL_ADMIN_REQUIRED';
    end if;

    insert into public.token_purchases (
        provider,
        provider_purchase_id,
        provider_event_id,
        telegram_user_id,
        tokens,
        amount_paid,
        currency,
        verified_by_telegram_user_id
    )
    values (
        p_provider,
        trim(p_provider_purchase_id),
        nullif(trim(p_provider_event_id), ''),
        p_telegram_user_id,
        p_tokens,
        p_amount_paid,
        'USD',
        p_verified_by_telegram_user_id
    )
    on conflict do nothing
    returning id into new_purchase_id;

    if new_purchase_id is null then
        select token_balance into remaining_tokens
        from public.users
        where telegram_user_id = p_telegram_user_id;

        return jsonb_build_object(
            'credited', false,
            'duplicate', true,
            'token_balance', coalesce(remaining_tokens, 0),
            'commission', 0
        );
    end if;

    update public.users
    set token_balance = token_balance + p_tokens,
        updated_at = now()
    where telegram_user_id = p_telegram_user_id
    returning referred_by_telegram_user_id, token_balance
    into referrer_id, remaining_tokens;

    if not found then
        raise exception 'TRADE_USER_NOT_FOUND';
    end if;

    if referrer_id is not null and referrer_id <> p_telegram_user_id then
        new_commission := round(p_amount_paid * 0.05, 2);
        insert into public.referral_commissions (
            purchase_id,
            referrer_telegram_user_id,
            buyer_telegram_user_id,
            amount_paid,
            commission_amount,
            currency
        )
        values (
            new_purchase_id,
            referrer_id,
            p_telegram_user_id,
            p_amount_paid,
            new_commission,
            'USD'
        );

        update public.users
        set referral_balance = referral_balance + new_commission,
            updated_at = now()
        where telegram_user_id = referrer_id;
    end if;

    return jsonb_build_object(
        'credited', true,
        'duplicate', false,
        'token_balance', remaining_tokens,
        'referrer_telegram_user_id', referrer_id,
        'commission', new_commission
    );
end;
$$;

create or replace function public.create_referral_withdrawal(
    p_telegram_user_id text,
    p_payout_details_snapshot_encrypted text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    user_balance numeric(18, 2);
    today_utc date := (now() at time zone 'UTC')::date;
    month_start date := date_trunc('month', now() at time zone 'UTC')::date;
    month_end date := (date_trunc('month', now() at time zone 'UTC')
                       + interval '1 month - 1 day')::date;
    payout_date date;
    withdrawal_id uuid;
begin
    if p_payout_details_snapshot_encrypted is null
       or length(p_payout_details_snapshot_encrypted) = 0 then
        raise exception 'PAYOUT_DETAILS_REQUIRED';
    end if;

    select referral_balance into user_balance
    from public.users
    where telegram_user_id = p_telegram_user_id
    for update;

    if not found then
        raise exception 'TRADE_USER_NOT_FOUND';
    end if;
    if exists (
        select 1 from public.referral_withdrawals
        where telegram_user_id = p_telegram_user_id
          and status = 'pending'
    ) then
        raise exception 'REFERRAL_WITHDRAWAL_ALREADY_PENDING';
    end if;
    if user_balance < 10 then
        raise exception 'REFERRAL_MINIMUM_WITHDRAWAL_NOT_MET';
    end if;

    payout_date := case
        when extract(day from today_utc) <= 15
            then month_start + interval '14 days'
        when today_utc = month_end
            then today_utc
        else month_end
    end;

    update public.users
    set referral_balance = 0,
        updated_at = now()
    where telegram_user_id = p_telegram_user_id;

    insert into public.referral_withdrawals (
        telegram_user_id,
        amount,
        scheduled_for,
        payout_details_snapshot_encrypted
    )
    values (
        p_telegram_user_id,
        user_balance,
        payout_date,
        p_payout_details_snapshot_encrypted
    )
    returning id into withdrawal_id;

    return jsonb_build_object(
        'withdrawal_id', withdrawal_id,
        'amount', user_balance,
        'scheduled_for', payout_date,
        'status', 'pending'
    );
end;
$$;

create or replace function public.admin_update_referral_withdrawal(
    p_admin_telegram_user_id text,
    p_withdrawal_id uuid,
    p_status text,
    p_failure_reason text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    withdrawal_row public.referral_withdrawals%rowtype;
begin
    if not exists (
        select 1 from public.users
        where telegram_user_id = p_admin_telegram_user_id
          and is_admin = true
    ) then
        raise exception 'REFERRAL_ADMIN_REQUIRED';
    end if;
    if p_status not in ('paid', 'failed')
       or (p_status = 'failed' and length(trim(coalesce(p_failure_reason, ''))) = 0) then
        raise exception 'REFERRAL_WITHDRAWAL_STATUS_INVALID';
    end if;

    update public.referral_withdrawals
    set status = p_status,
        failure_reason = case when p_status = 'failed'
                              then trim(p_failure_reason) else null end,
        paid_at = case when p_status = 'paid' then now() else null end,
        updated_at = now()
    where id = p_withdrawal_id
      and status = 'pending'
    returning * into withdrawal_row;

    if not found then
        raise exception 'REFERRAL_WITHDRAWAL_NOT_PENDING';
    end if;

    if p_status = 'failed' then
        update public.users
        set referral_balance = referral_balance + withdrawal_row.amount,
            updated_at = now()
        where telegram_user_id = withdrawal_row.telegram_user_id;
    end if;

    return jsonb_build_object(
        'withdrawal_id', withdrawal_row.id,
        'telegram_user_id', withdrawal_row.telegram_user_id,
        'amount', withdrawal_row.amount,
        'currency', withdrawal_row.currency,
        'status', withdrawal_row.status,
        'failure_reason', withdrawal_row.failure_reason
    );
end;
$$;

create or replace function public.get_referral_summary(
    p_telegram_user_id text
)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
    select jsonb_build_object(
        'referral_count', (
            select count(*)::integer
            from public.users
            where referred_by_telegram_user_id = p_telegram_user_id
        ),
        'total_earned', coalesce((
            select sum(commission_amount)
            from public.referral_commissions
            where referrer_telegram_user_id = p_telegram_user_id
        ), 0),
        'available_balance', coalesce((
            select referral_balance
            from public.users
            where telegram_user_id = p_telegram_user_id
        ), 0),
        'currency', 'USD'
    );
$$;

create or replace function public.get_pending_referral_withdrawals(
    p_admin_telegram_user_id text
)
returns setof jsonb
language plpgsql
security definer
set search_path = public
as $$
begin
    if not exists (
        select 1 from public.users
        where telegram_user_id = p_admin_telegram_user_id
          and is_admin = true
    ) then
        raise exception 'REFERRAL_ADMIN_REQUIRED';
    end if;

    return query
    select jsonb_build_object(
        'id', w.id,
        'telegram_user_id', w.telegram_user_id,
        'amount', w.amount,
        'currency', w.currency,
        'status', w.status,
        'requested_at', w.requested_at,
        'scheduled_for', w.scheduled_for,
        'payout_country', u.payout_country,
        'payout_details_encrypted', w.payout_details_snapshot_encrypted
    )
    from public.referral_withdrawals w
    join public.users u using (telegram_user_id)
    where w.status = 'pending'
      and w.scheduled_for <= (now() at time zone 'UTC')::date
    order by w.scheduled_for, w.requested_at;
end;
$$;

revoke all on function public.claim_welcome_gift_200(text)
    from public, anon, authenticated;
revoke all on function public.attach_referral(text, text)
    from public, anon, authenticated;
revoke all on function public.record_verified_token_purchase(
    text, text, text, text, integer, numeric, text, text
) from public, anon, authenticated;
revoke all on function public.create_referral_withdrawal(text, text)
    from public, anon, authenticated;
revoke all on function public.admin_update_referral_withdrawal(text, uuid, text, text)
    from public, anon, authenticated;
revoke all on function public.get_referral_summary(text)
    from public, anon, authenticated;
revoke all on function public.get_pending_referral_withdrawals(text)
    from public, anon, authenticated;

grant execute on function public.claim_welcome_gift_200(text) to service_role;
grant execute on function public.attach_referral(text, text) to service_role;
grant execute on function public.record_verified_token_purchase(
    text, text, text, text, integer, numeric, text, text
) to service_role;
grant execute on function public.create_referral_withdrawal(text, text) to service_role;
grant execute on function public.admin_update_referral_withdrawal(text, uuid, text, text)
    to service_role;
grant execute on function public.get_referral_summary(text) to service_role;
grant execute on function public.get_pending_referral_withdrawals(text)
    to service_role;

create or replace function public.grant_first_auth_tokens(
    p_telegram_user_id text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    remaining_tokens integer;
begin
    select token_balance into remaining_tokens
    from public.users
    where telegram_user_id = p_telegram_user_id;

    if not found then
        raise exception 'TRADE_USER_NOT_FOUND';
    end if;

    return jsonb_build_object(
        'granted', false,
        'tokens_granted', 0,
        'token_balance', remaining_tokens
    );
end;
$$;

revoke all on function public.grant_first_auth_tokens(text)
    from public, anon, authenticated;
grant execute on function public.grant_first_auth_tokens(text) to service_role;
