-- Create index for join on electioneering costs
create index on sched_b (link_id);

-- Create queue tables to hold changes to Schedule B
drop table if exists ofec_sched_b_queue_new;
drop table if exists ofec_sched_b_queue_old;
create table ofec_sched_b_queue_new as select * from sched_b limit 0;
create table ofec_sched_b_queue_old as select * from sched_b limit 0;
alter table ofec_sched_b_queue_new add column timestamp timestamp;
alter table ofec_sched_b_queue_old add column timestamp timestamp;
alter table ofec_sched_b_queue_new add column two_year_transaction_period smallint;
alter table ofec_sched_b_queue_old add column two_year_transaction_period smallint;
create index on ofec_sched_b_queue_new (sched_b_sk);
create index on ofec_sched_b_queue_old (sched_b_sk);
create index on ofec_sched_b_queue_new (timestamp);
create index on ofec_sched_b_queue_old (timestamp);
create index on ofec_sched_b_queue_new (two_year_transaction_period);
create index on ofec_sched_b_queue_old (two_year_transaction_period);

-- Create trigger to maintain Schedule B queues
create or replace function ofec_sched_b_update_queues() returns trigger as $$
declare
    start_year int = TG_ARGV[0]::int;
    two_year_transaction_period_new smallint;
    two_year_transaction_period_old smallint;
begin
    two_year_transaction_period_new = get_transaction_year(new.disb_dt, new.rpt_yr);
    two_year_transaction_period_old = get_transaction_year(old.disb_dt, old.rpt_yr);

    if tg_op = 'INSERT' then
        if two_year_transaction_period_new >= start_year then
            delete from ofec_sched_b_queue_new where sched_b_sk = new.sched_b_sk;
            insert into ofec_sched_b_queue_new values (new.*);
        end if;
        return new;
    elsif tg_op = 'UPDATE' then
        if two_year_transaction_period_new >= start_year then
            delete from ofec_sched_b_queue_new where sched_b_sk = new.sched_b_sk;
            delete from ofec_sched_b_queue_old where sched_b_sk = old.sched_b_sk;
            insert into ofec_sched_b_queue_new values (new.*, timestamp, two_year_transaction_period_new);
            insert into ofec_sched_b_queue_old values (old.*, timestamp, two_year_transaction_period_old);
        end if;
        return new;
    elsif tg_op = 'DELETE' then
        if two_year_transaction_period_old >= start_year then
            delete from ofec_sched_b_queue_old where sched_b_sk = old.sched_b_sk;
            insert into ofec_sched_b_queue_old values (old.*, timestamp, two_year_transaction_period_old);
        end if;
        return old;
    end if;
end
$$ language plpgsql;

drop trigger if exists ofec_sched_b_queue_trigger on sched_b;
create trigger ofec_sched_b_queue_trigger before insert or update or delete
    on sched_b for each row execute procedure ofec_sched_b_update_queues(:START_YEAR_AGGREGATE)
;
