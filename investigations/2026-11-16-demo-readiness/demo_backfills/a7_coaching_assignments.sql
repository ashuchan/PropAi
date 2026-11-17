\set ON_ERROR_STOP 1
begin;
with plan(associate_id, community_id, skill_key, lesson_id, lesson_title, status, qr, qo, days_ago) as (values
 ('d30ef3ed-b38d-54e7-9653-e743f895c848','cad6d9d4-5881-40ed-9727-73b60ad1a375','closing','summary','Summary close','completed',4,5,9),
 ('d30ef3ed-b38d-54e7-9653-e743f895c848','cad6d9d4-5881-40ed-9727-73b60ad1a375','discovery','discovery','Discovery questions','enabled',null,null,2),
 ('7d3a2c61-b871-5e5c-b2e8-7565d23350ad','f585b747-ff97-4d43-987b-1e0882f029eb','follow-up','leave','Leave with a next step','completed',4,4,8),
 ('7d3a2c61-b871-5e5c-b2e8-7565d23350ad','f585b747-ff97-4d43-987b-1e0882f029eb','closing','direct','Direct close: hear the signal','in_progress',null,null,3),
 ('355de48a-a11c-576a-8c61-39855fdcb83b','24589e94-c8a1-419d-95fe-314a0aa14332','bond','first_five','The first five minutes','completed',5,5,10),
 ('355de48a-a11c-576a-8c61-39855fdcb83b','24589e94-c8a1-419d-95fe-314a0aa14332','concerns','money','Money: rent, fees and deposit','enabled',null,null,1),
 ('125a3aa9-f740-5672-a60c-8b2248f3aa72','85ec7ed8-5d5f-49cb-af16-e8c34aced869','closing','consult','Consultative close','in_progress',null,null,4),
 ('125a3aa9-f740-5672-a60c-8b2248f3aa72','85ec7ed8-5d5f-49cb-af16-e8c34aced869','fair-housing','fh_same','The same for everyone','completed',4,5,12),
 ('048cf401-e9a1-5f62-b201-63c29c832567','076a07bd-beec-4e8b-85a4-afee64fd6ab4','discovery','discovery','Discovery questions','completed',5,5,7),
 ('048cf401-e9a1-5f62-b201-63c29c832567','076a07bd-beec-4e8b-85a4-afee64fd6ab4','concerns','not_yes','When it''s not a yes','enabled',null,null,2),
 ('6edb8d09-0616-5dce-8c60-72c607a2ebed','9254d356-a920-5e47-8793-e5f1808c4d41','concerns','shopping','"I need to see other places"','in_progress',null,null,3),
 ('6edb8d09-0616-5dce-8c60-72c607a2ebed','9254d356-a920-5e47-8793-e5f1808c4d41','fair-housing','fh_home','Talk about the home, not the person','completed',3,4,11))
insert into tm_lesson_assignments (id, company_id, community_id, associate_id, skill_key, lesson_id, lesson_title, evidence, status, enabled_by, enabled_at, started_at, completed_at, quiz_right, quiz_of, quiz_attempts, created_at, updated_at, due_on, source)
select md5('dsuf-a7:'||associate_id||':'||lesson_id)::uuid, '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec', community_id::uuid, associate_id::uuid, skill_key, lesson_id, lesson_title, '{"source": "library"}'::jsonb, status,
       'ae8c95ca-1e26-4d0f-817f-f53757624b48', now()-make_interval(days=>days_ago), case when status<>'enabled' then now()-make_interval(days=>days_ago-1) end,
       case when status='completed' then now()-make_interval(days=>greatest(days_ago-3,0)) end, qr, qo, case when status='completed' then 1 else 0 end,
       now()-make_interval(days=>days_ago), now(), (current_date + 14 - days_ago), null
from plan where not exists (select 1 from tm_lesson_assignments x where x.associate_id=plan.associate_id::uuid and x.lesson_id=plan.lesson_id)
on conflict (id) do nothing
returning id;
commit;
