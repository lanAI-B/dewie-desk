-- Why do replies open new conversations instead of joining their thread?
-- Read-only. Run (Git Bash, from dewie-desk):
--   docker exec -i dewie-desk-postgres-1 psql -U postgres -d chatwoot -v days=14 < scripts/threading_check.sql
-- Notes: messages.content_attributes is a JSON *string* holding JSON, hence #>> '{}'.
-- Inboxes 1 (ABS Service) and 3 (ACTEX Inbox). Inbox 2 is Lana's own mail: not the desk.

\echo '== 1. New conversations by how their first message relates to a thread'
with firsts as (
  select distinct on (c.id) c.id conv, c.inbox_id, c.contact_id,
    ((m.content_attributes #>> '{}')::jsonb)->'email'->>'in_reply_to' irt,
    ((m.content_attributes #>> '{}')::jsonb)->'email'->>'subject' subj
  from conversations c join messages m on m.conversation_id = c.id
  where c.created_at > now() - (:'days' || ' days')::interval and c.inbox_id in (1, 3)
  order by c.id, m.created_at)
select
  count(*) as total_new,
  count(*) filter (where coalesce(irt, '') = '') as fresh_email,
  count(*) filter (where irt <> '' and exists (select 1 from messages p
      where p.source_id in (f.irt, trim(both '<>' from f.irt)))) as reply_parent_known,
  count(*) filter (where irt <> '' and not exists (select 1 from messages p
      where p.source_id in (f.irt, trim(both '<>' from f.irt)))) as reply_parent_unknown,
  count(*) filter (where subj ~* '^(re|aw|fw|fwd):' and exists (select 1 from conversations o
      where o.contact_id = f.contact_id and o.id < f.conv and o.inbox_id = f.inbox_id))
      as re_subject_contact_has_older
from firsts f;

\echo '== 2. Unthreaded replies: whose mail were they replying to? (domain of In-Reply-To)'
with firsts as (
  select distinct on (c.id) c.id conv,
    ((m.content_attributes #>> '{}')::jsonb)->'email'->>'in_reply_to' irt
  from conversations c join messages m on m.conversation_id = c.id
  where c.created_at > now() - (:'days' || ' days')::interval and c.inbox_id in (1, 3)
  order by c.id, m.created_at)
select regexp_replace(irt, '^.*@([^>]+)>?$', '\1') as replied_to_host, count(*)
from firsts f
where irt <> '' and not exists (select 1 from messages p
    where p.source_id in (f.irt, trim(both '<>' from f.irt)))
group by 1 order by 2 desc limit 15;

\echo '== 3. Parent known but still a new conversation (should have threaded)'
with firsts as (
  select distinct on (c.id) c.id conv, c.created_at,
    ((m.content_attributes #>> '{}')::jsonb)->'email'->>'in_reply_to' irt
  from conversations c join messages m on m.conversation_id = c.id
  where c.created_at > now() - (:'days' || ' days')::interval and c.inbox_id in (1, 3)
  order by c.id, m.created_at)
select f.conv, p.conversation_id as parent_conv, pc.status as parent_status_now, pc.inbox_id,
  to_char(f.created_at at time zone 'America/New_York', 'MM-DD HH24:MI') as child_created
from firsts f
join messages p on p.source_id in (f.irt, trim(both '<>' from f.irt))
join conversations pc on pc.id = p.conversation_id
where p.conversation_id <> f.conv;

\echo '== 4. Outgoing messages that carry a Message-ID Chatwoot can thread on'
select count(*) as outgoing,
  count(*) filter (where coalesce(source_id, '') <> '') as with_message_id
from messages
where created_at > now() - (:'days' || ' days')::interval and message_type = 1;
