alter table public.users
    add column if not exists welcome_tokens_granted_at timestamptz;

-- Existing profiles may have authenticated before this grant was introduced.
-- Preserve their current token balances and prevent a reauthentication bonus.
update public.users
set welcome_tokens_granted_at = coalesce(created_at, now())
where welcome_tokens_granted_at is null;

alter table public.users
    alter column token_balance set default 0;

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
    update public.users
    set token_balance = token_balance + 150,
        welcome_tokens_granted_at = now(),
        updated_at = now()
    where telegram_user_id = p_telegram_user_id
      and welcome_tokens_granted_at is null
    returning token_balance into remaining_tokens;

    if found then
        return jsonb_build_object(
            'granted', true,
            'tokens_granted', 150,
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

revoke all on function public.grant_first_auth_tokens(text)
    from public, anon, authenticated;
grant execute on function public.grant_first_auth_tokens(text)
    to service_role;
