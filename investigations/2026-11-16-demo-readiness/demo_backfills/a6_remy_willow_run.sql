\set ON_ERROR_STOP 1
insert into associate_community_map (id, associate_id, community_id, is_active)
select md5('dsuf-a6:remy-wr')::uuid, '2109d24c-10e4-4968-953e-0187bcc8c899', '24589e94-c8a1-419d-95fe-314a0aa14332', true
where not exists (select 1 from associate_community_map where associate_id='2109d24c-10e4-4968-953e-0187bcc8c899' and community_id='24589e94-c8a1-419d-95fe-314a0aa14332')
on conflict (id) do nothing;
