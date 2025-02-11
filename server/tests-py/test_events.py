#!/usr/bin/env python3

import pytest
import queue
import time
import utils
from validate import check_query_f, check_event

usefixtures = pytest.mark.usefixtures

# Every test in this class requires the events webhook to be running first
# We are also going to mark as server upgrade tests are allowed
# A few tests are going to be excluded with skip_server_upgrade_test mark
pytestmark = [usefixtures('evts_webhook'), pytest.mark.allow_server_upgrade_test]

def select_last_event_fromdb(hge_ctx):
    q = {
        "type": "select",
        "args": {
            "table": {"schema": "hdb_catalog", "name": "event_log"},
            "columns": ["*"],
            "order_by": ["-created_at"],
            "limit": 1
        }
    }
    st_code, resp = hge_ctx.v1q(q)
    return st_code, resp


def insert(hge_ctx, table, row, returning=[], headers = {}):
    return insert_many(hge_ctx, table, [row], returning, headers)

def insert_many(hge_ctx, table, rows, returning=[], headers = {}):
    q = {
        "type": "insert",
        "args": {
            "table": table,
            "objects": rows,
            "returning": returning
        }
    }
    st_code, resp = hge_ctx.v1q(q, headers = headers)
    return st_code, resp


def update(hge_ctx, table, where_exp, set_exp, headers = {}):
    q = {
        "type": "update",
        "args": {
            "table": table,
            "where": where_exp,
            "$set": set_exp
        }
    }
    st_code, resp = hge_ctx.v1q(q, headers = headers)
    return st_code, resp


def delete(hge_ctx, table, where_exp, headers = {}):
    q = {
        "type": "delete",
        "args": {
            "table": table,
            "where": where_exp
        }
    }
    st_code, resp = hge_ctx.v1q(q, headers = headers)
    return st_code, resp

@usefixtures("per_method_tests_db_state")
class TestCreateAndDelete:

    def test_create_delete(self, hge_ctx):
        check_query_f(hge_ctx, self.dir() + "/create_and_delete.yaml")

    def test_create_reset(self, hge_ctx):
        check_query_f(hge_ctx, self.dir() + "/create_and_reset.yaml")

    # Can't run server upgrade tests, as this test has a schema change
    @pytest.mark.skip_server_upgrade_test
    def test_create_operation_spec_not_provider_err(self, hge_ctx):
        check_query_f(hge_ctx, self.dir() + "/create_trigger_operation_specs_not_provided_err.yaml")

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/create-delete'

# Generates a backlog of events, then:
# - checks that we're processing with the concurrency and backpressure
#   characteristics we expect 
# - ensures all events are successfully processed
#
# NOTE: this expects:
#   HASURA_GRAPHQL_EVENTS_HTTP_POOL_SIZE=8
#   HASURA_GRAPHQL_EVENTS_FETCH_BATCH_SIZE=100  (the default)
@usefixtures("per_method_tests_db_state")
class TestEventFlood(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/flood'

    def test_flood(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_flood"}

        # Trigger a bunch of events; hasura will begin processing but block on /block
        payload = range(1,1001)
        rows = list(map(lambda x: {"c1": x, "c2": "hello"}, payload))
        st_code, resp = insert_many(hge_ctx, table, rows)
        assert st_code == 200, resp

        def check_backpressure():
            # Expect that HASURA_GRAPHQL_EVENTS_HTTP_POOL_SIZE webhooks are pending:
            assert evts_webhook.blocked_count == 8
            # ...Great, so presumably: 
            # - event handlers are run concurrently
            # - with concurrency limited by HASURA_GRAPHQL_EVENTS_HTTP_POOL_SIZE

            locked_counts = {
                "type":"run_sql",
                "args":{
                    "sql":'''
                    select 
                      (select count(*) from hdb_catalog.event_log where locked IS NOT NULL) as num_locked,
                      count(*) as total
                    from hdb_catalog.event_log 
                    where table_name = 'test_flood'
                    '''
                }
            }
            st, resp = hge_ctx.v1q(locked_counts)
            assert st == 200, resp
            # Make sure we have 2*HASURA_GRAPHQL_EVENTS_FETCH_BATCH_SIZE events checked out:
            #  - 100 prefetched
            #  - 100 being processed right now (but blocked on HTTP_POOL capacity)
            # TODO it seems like we have some shared state in CI causing this to fail when we check 1000 below
            assert resp['result'][1][0] == '200'
            # assert resp['result'][1] == ['200', '1000']

        # Rather than sleep arbitrarily, loop until assertions pass:
        utils.until_asserts_pass(30, check_backpressure)
        # ...then make sure we're truly stable:
        time.sleep(3)
        check_backpressure()

        # unblock open and future requests to /block; check all events processed
        evts_webhook.unblock()

        def get_evt():
            # TODO ThreadedHTTPServer helps locally (I only need a timeout of
            # 10 here), but we still need a bit of a long timeout here for CI
            # it seems, since webhook can't keep up there:
            ev_full = evts_webhook.get_event(600)
            return ev_full['body']['event']['data']['new']['c1']
        # Make sure we got all payloads (probably out of order):
        ns = list(map(lambda _: get_evt(), payload))
        ns.sort()
        assert ns == list(payload)

@usefixtures("per_class_tests_db_state")
class TestEventDataFormat(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/data_format'
    
    def test_bigint(self, hge_ctx, evts_webhook):
      table = {"schema": "hge_tests", "name": "test_bigint"}

      init_row = {"id": 50755254975729665, "name": "hello"}
      exp_ev_data = {
          "old": None,
          "new": {"id": "50755254975729665", "name": "hello"}
      }
      st_code, resp = insert(hge_ctx, table, init_row)
      assert st_code == 200, resp
      check_event(hge_ctx, evts_webhook, "bigint_all", table, "INSERT", exp_ev_data)
    
    def test_geojson(self, hge_ctx, evts_webhook):
      table = {"schema": "hge_tests", "name": "test_geojson"}

      exp_ev_data = {
          "old": {  "id" : 1,
                    "location":{
                        "coordinates":[
                          -43.77,
                          45.64
                        ],
                        "crs":{
                          "type":"name",
                          "properties":{
                              "name":"urn:ogc:def:crs:EPSG::4326"
                          }
                        },
                        "type":"Point"
                    }
                  },
          "new": {  "id": 2,
                    "location":{
                        "coordinates":[
                          -43.77,
                          45.64
                        ],
                        "crs":{
                          "type":"name",
                          "properties":{
                              "name":"urn:ogc:def:crs:EPSG::4326"
                          }
                        },
                        "type":"Point"
                    }
                  }
      }
      

      where_exp = {"id" : 1}
      set_exp = {"id": 2}
      st_code, resp = update(hge_ctx, table, where_exp, set_exp)
      assert st_code == 200, resp
      check_event(hge_ctx, evts_webhook, "geojson_all", table, "UPDATE", exp_ev_data)  



@usefixtures("per_class_tests_db_state")
class TestCreateEvtQuery(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/basic'

    def test_basic(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "INSERT", exp_ev_data)

        where_exp = {"c1": 1}
        set_exp = {"c2": "world"}
        exp_ev_data = {
            "old": init_row,
            "new": {"c1": 1, "c2": "world"}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "UPDATE", exp_ev_data)

        exp_ev_data = {
            "old": {"c1": 1, "c2": "world"},
            "new": None
        }
        st_code, resp = delete(hge_ctx, table, where_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "DELETE", exp_ev_data)

    def test_partitioned_table_basic_insert(self, hge_ctx, evts_webhook):
        if hge_ctx.pg_version < 110000:
            pytest.skip('Event triggers on partioned tables are not supported in Postgres versions < 11')
            return
        st_code, resp = hge_ctx.v1q_f(self.dir() + '/partition_table_setup.yaml')
        assert st_code == 200, resp
        table = { "schema":"hge_tests", "name": "measurement"}

        init_row = { "city_id": 1, "logdate": "2006-02-02", "peaktemp": 1, "unitsales": 1}

        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "measurement_all", table, "INSERT", exp_ev_data)
        st_code, resp = hge_ctx.v1q_f(self.dir() + '/partition_table_teardown.yaml')
        assert st_code == 200, resp

@usefixtures('per_method_tests_db_state')
class TestRetryConf(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/retry_conf'

    # webhook: http://127.0.0.1:5592/fail
    # retry_conf:
    #   num_retries: 4
    #   interval_sec: 1
    def test_basic(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_retry", table, "INSERT", exp_ev_data, webhook_path = "/fail", retry = 0)
        check_event(hge_ctx, evts_webhook, "t1_retry", table, "INSERT", exp_ev_data, webhook_path = "/fail", retry = 1)
        check_event(hge_ctx, evts_webhook, "t1_retry", table, "INSERT", exp_ev_data, webhook_path = "/fail", retry = 2)
        check_event(hge_ctx, evts_webhook, "t1_retry", table, "INSERT", exp_ev_data, webhook_path = "/fail", retry = 3)
        check_event(hge_ctx, evts_webhook, "t1_retry", table, "INSERT", exp_ev_data, webhook_path = "/fail", retry = 4)

    # webhook: http://127.0.0.1:5592/sleep_2s
    # retry_conf:
    #   num_retries: 2
    #   interval_sec: 1
    #   timeout_sec: 1
    def test_timeout_short(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t2"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t2_timeout_short", table, "INSERT", exp_ev_data, webhook_path = "/sleep_2s", retry = 0, get_timeout = 5)
        check_event(hge_ctx, evts_webhook, "t2_timeout_short", table, "INSERT", exp_ev_data, webhook_path = "/sleep_2s", retry = 1, get_timeout = 5)
        check_event(hge_ctx, evts_webhook, "t2_timeout_short", table, "INSERT", exp_ev_data, webhook_path = "/sleep_2s", retry = 2, get_timeout = 5)

    # webhook: http://127.0.0.1:5592/sleep_2s
    # retry_conf:
    #   num_retries: 0
    #   interval_sec: 2
    #   timeout_sec: 10
    def test_timeout_long(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t3"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        time.sleep(2)
        check_event(hge_ctx, evts_webhook, "t3_timeout_long", table, "INSERT", exp_ev_data, webhook_path = "/sleep_2s")

    # Keep this one last
    def test_queue_empty(self, hge_ctx, evts_webhook):
        try:
            evts_webhook.get_event(3)
            assert False, "expected queue to be empty"
        except queue.Empty:
            pass

@usefixtures('per_method_tests_db_state')
class TestEvtHeaders(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/headers'

    def test_basic(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        headers = {"X-Header-From-Value": "MyValue", "X-Header-From-Env": "MyEnvValue"}
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "INSERT", exp_ev_data, headers = headers)

class TestUpdateEvtQuery(object):

    @pytest.fixture(autouse=True)
    def transact(self, request, hge_ctx, evts_webhook):
        print("In setup method")
        # Adds trigger on 'test_t1' with...
        #   insert:
        #     columns: '*'
        #   update:
        #     columns: [c2, c3]
        st_code, resp = hge_ctx.v1q_f('queries/event_triggers/update_query/create-setup.yaml')
        assert st_code == 200, resp
        # overwrites trigger added above, with...
        #   delete:
        #     columns: "*"
        #   update:
        #     columns: ["c1", "c3"]
        st_code, resp = hge_ctx.v1q_f('queries/event_triggers/update_query/update-setup.yaml')
        assert st_code == 200, '{}'.format(resp)
        assert resp[1]["sources"][0]["tables"][0]["event_triggers"][0]["webhook"] == 'http://127.0.0.1:5592/new'
        yield
        st_code, resp = hge_ctx.v1q_f('queries/event_triggers/update_query/teardown.yaml')
        assert st_code == 200, resp

    def test_update_basic(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        # Expect that inserting a row (which would have triggered in original
        # create_event_trigger) does not trigger
        init_row = {"c1": 1, "c2": "hello", "c3": {"name": "clarke"}}
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        with pytest.raises(queue.Empty):
            check_event(hge_ctx, evts_webhook, "t1_cols", table, "INSERT", {}, webhook_path = "/new", get_timeout = 0)

        # Likewise for an update on c2:
        where_exp = {"c1": 1}
        set_exp = {"c2": "world"}
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        with pytest.raises(queue.Empty):
            check_event(hge_ctx, evts_webhook, "t1_cols", table, "UPDATE", {}, webhook_path = "/new", get_timeout = 0)

        where_exp = {"c1": 1}
        set_exp = {"c3": {"name": "bellamy"}}
        exp_ev_data = {
            "old": {"c1": 1, "c2": "world", "c3": {"name": "clarke"}},
            "new": {"c1": 1, "c2": "world", "c3": {"name": "bellamy"}}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_cols", table, "UPDATE", exp_ev_data, webhook_path ="/new")

        where_exp = {"c1": 1}
        set_exp = {"c1": 2}
        exp_ev_data = {
            "old": {"c1": 1, "c2": "world", "c3": {"name": "bellamy"}},
            "new": {"c1": 2, "c2": "world", "c3": {"name": "bellamy"}}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_cols", table, "UPDATE", exp_ev_data, webhook_path ="/new")

        where_exp = {"c1": 2}
        exp_ev_data = {
            "old": {"c1": 2, "c2": "world", "c3": {"name": "bellamy"}},
            "new": None
        }
        st_code, resp = delete(hge_ctx, table, where_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_cols", table, "DELETE", exp_ev_data, webhook_path = "/new")

@usefixtures('per_method_tests_db_state')
class TestDeleteEvtQuery(object):

    directory = 'queries/event_triggers'

    setup_files = [
        directory + '/basic/setup.yaml',
        directory + '/delete_query/setup.yaml'
    ]

    teardown_files = [ directory + '/delete_query/teardown.yaml']

    # Ensure deleting an event trigger works
    def test_delete_basic(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        with pytest.raises(queue.Empty):
            check_event(hge_ctx, evts_webhook, "t1_all", table, "INSERT", exp_ev_data, get_timeout=0)

        where_exp = {"c1": 1}
        set_exp = {"c2": "world"}
        exp_ev_data = {
            "old": init_row,
            "new": {"c1": 1, "c2": "world"}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        with pytest.raises(queue.Empty):
            check_event(hge_ctx, evts_webhook, "t1_all", table, "UPDATE", exp_ev_data, get_timeout=0)

        exp_ev_data = {
            "old": {"c1": 1, "c2": "world"},
            "new": None
        }
        st_code, resp = delete(hge_ctx, table, where_exp)
        assert st_code == 200, resp
        with pytest.raises(queue.Empty):
            # NOTE: use a bit of a delay here, to catch any stray events generated above
            check_event(hge_ctx, evts_webhook, "t1_all", table, "DELETE", exp_ev_data, get_timeout=2)

@usefixtures('per_class_tests_db_state')
class TestEvtSelCols:

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/selected_cols'

    def test_selected_cols(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": {"c1": 1, "c2": "hello"}
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_cols", table, "INSERT", exp_ev_data)

        where_exp = {"c1": 1}
        set_exp = {"c2": "world"}
        # expected no event hence previous expected data
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        with pytest.raises(queue.Empty):
            check_event(hge_ctx, evts_webhook, "t1_cols", table, "UPDATE", exp_ev_data, get_timeout=0)

        where_exp = {"c1": 1}
        set_exp = {"c1": 2}
        exp_ev_data = {
            "old": {"c1": 1, "c2": "world"},
            "new": {"c1": 2, "c2": "world"}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_cols", table, "UPDATE", exp_ev_data)

        where_exp = {"c1": 2}
        exp_ev_data = {
            "old": {"c1": 2, "c2": "world"},
            "new": None
        }
        st_code, resp = delete(hge_ctx, table, where_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_cols", table, "DELETE", exp_ev_data)

    @pytest.mark.skip_server_upgrade_test
    def test_selected_cols_dep(self, hge_ctx, evts_webhook):
        st_code, resp = hge_ctx.v1q({
            "type": "run_sql",
            "args": {
                "sql": "alter table hge_tests.test_t1 drop column c1"
            }
        })
        assert st_code == 400, resp
        assert resp['code'] == "dependency-error", resp

        st_code, resp = hge_ctx.v1q({
            "type": "run_sql",
            "args": {
                "sql": "alter table hge_tests.test_t1 drop column c2"
            }
        })
        assert st_code == 200, resp

@usefixtures('per_method_tests_db_state')
class TestEvtInsertOnly:

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/insert_only'

    def test_insert_only(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_insert", table, "INSERT", exp_ev_data)

        where_exp = {"c1": 1}
        set_exp = {"c2": "world"}
        exp_ev_data = {
            "old": init_row,
            "new": {"c1": 1, "c2": "world"}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        with pytest.raises(queue.Empty):
            check_event(hge_ctx, evts_webhook, "t1_insert", table, "UPDATE", exp_ev_data, get_timeout=0)

        exp_ev_data = {
            "old": {"c1": 1, "c2": "world"},
            "new": None
        }
        st_code, resp = delete(hge_ctx, table, where_exp)
        assert st_code == 200, resp
        with pytest.raises(queue.Empty):
            # NOTE: use a bit of a delay here, to catch any stray events generated above
            check_event(hge_ctx, evts_webhook, "t1_insert", table, "DELETE", exp_ev_data, get_timeout=2)


@usefixtures('per_class_tests_db_state')
class TestEvtSelPayload:

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/selected_payload'

    def test_selected_payload(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": {"c1": 1, "c2": "hello"}
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_payload", table, "INSERT", exp_ev_data)

        where_exp = {"c1": 1}
        set_exp = {"c2": "world"}
        exp_ev_data = {
            "old": {"c1": 1},
            "new": {"c1": 1}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_payload", table, "UPDATE", exp_ev_data)

        where_exp = {"c1": 1}
        set_exp = {"c1": 2}
        exp_ev_data = {
            "old": {"c1": 1},
            "new": {"c1": 2}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_payload", table, "UPDATE", exp_ev_data)

        where_exp = {"c1": 2}
        exp_ev_data = {
            "old": {"c2": "world"},
            "new": None
        }
        st_code, resp = delete(hge_ctx, table, where_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_payload", table, "DELETE", exp_ev_data)

    def test_selected_payload_dep(self, hge_ctx):
        st_code, resp = hge_ctx.v1q({
            "type": "run_sql",
            "args": {
                "sql": "alter table hge_tests.test_t1 drop column c1"
            }
        })
        assert st_code == 400, resp
        assert resp['code'] == "dependency-error", resp

        st_code, resp = hge_ctx.v1q({
            "type": "run_sql",
            "args": {
                "sql": "alter table hge_tests.test_t1 drop column c2"
            }
        })
        assert st_code == 400, resp
        assert resp['code'] == "dependency-error", resp

@usefixtures('per_method_tests_db_state')
class TestWebhookEnv(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/webhook_env'

    def test_basic(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "INSERT", exp_ev_data)

        where_exp = {"c1": 1}
        set_exp = {"c2": "world"}
        exp_ev_data = {
            "old": init_row,
            "new": {"c1": 1, "c2": "world"}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "UPDATE", exp_ev_data)

        exp_ev_data = {
            "old": {"c1": 1, "c2": "world"},
            "new": None
        }
        st_code, resp = delete(hge_ctx, table, where_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "DELETE", exp_ev_data)

@usefixtures('per_method_tests_db_state')
class TestWebhookTemplateURL(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/webhook_template_url'

    def test_basic(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        st_code, resp = insert(hge_ctx, table, init_row)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "INSERT", exp_ev_data, webhook_path = '/trigger')

        where_exp = {"c1": 1}
        set_exp = {"c2": "world"}
        exp_ev_data = {
            "old": init_row,
            "new": {"c1": 1, "c2": "world"}
        }
        st_code, resp = update(hge_ctx, table, where_exp, set_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "UPDATE", exp_ev_data, webhook_path = '/trigger')

        exp_ev_data = {
            "old": {"c1": 1, "c2": "world"},
            "new": None
        }
        st_code, resp = delete(hge_ctx, table, where_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "DELETE", exp_ev_data, webhook_path = '/trigger')

@usefixtures('per_method_tests_db_state')
class TestSessionVariables(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/basic'

    def test_basic(self, hge_ctx, evts_webhook):
        table = {"schema": "hge_tests", "name": "test_t1"}

        init_row = {"c1": 1, "c2": "hello"}
        exp_ev_data = {
            "old": None,
            "new": init_row
        }
        session_variables = { 'x-hasura-role': 'admin', 'x-hasura-allowed-roles': "['admin','user']", 'x-hasura-user-id': '1'}
        st_code, resp = insert(hge_ctx, table, init_row, headers = session_variables)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "INSERT", exp_ev_data, session_variables = session_variables)

        where_exp = {"c1": 1}
        set_exp = {"c2": "world"}
        exp_ev_data = {
            "old": init_row,
            "new": {"c1": 1, "c2": "world"}
        }
        session_variables = { 'x-hasura-role': 'admin', 'x-hasura-random': 'some_random_info', 'X-Random-Header': 'not_session_variable'}
        st_code, resp = update(hge_ctx, table, where_exp, set_exp, headers = session_variables)
        assert st_code == 200, resp
        session_variables.pop('X-Random-Header')
        check_event(hge_ctx, evts_webhook, "t1_all", table, "UPDATE", exp_ev_data, session_variables = session_variables)

        exp_ev_data = {
            "old": {"c1": 1, "c2": "world"},
            "new": None
        }
        st_code, resp = delete(hge_ctx, table, where_exp)
        assert st_code == 200, resp
        check_event(hge_ctx, evts_webhook, "t1_all", table, "DELETE", exp_ev_data)


@usefixtures('per_method_tests_db_state')
class TestManualEvents(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/manual_events'

    def test_basic(self, hge_ctx, evts_webhook):
        st_code, resp = hge_ctx.v1metadataq_f('queries/event_triggers/manual_events/enabled.yaml')
        assert st_code == 200, resp
        st_code, resp = hge_ctx.v1metadataq_f('queries/event_triggers/manual_events/disabled.yaml')
        assert st_code == 400, resp

    # This test is being added to ensure that the manual events
    # are not failing after any reload_metadata operation, this
    # has been an issue of concern in some of the recent releases(v2.0.1 onwards)
    def test_basic_with_reload_metadata(self, hge_ctx, evts_webhook):
        reload_metadata_q = {
            "type": "reload_metadata",
            "args": {
                "reload_sources": True
            }
        }

        for _ in range(5):
            self.test_basic(hge_ctx, evts_webhook)

            st_code, resp = hge_ctx.v1metadataq(reload_metadata_q)
            assert st_code == 200, resp

            self.test_basic(hge_ctx, evts_webhook)           

@usefixtures('per_method_tests_db_state')
class TestEventsAsynchronousExecution(object):

    @classmethod
    def dir(cls):
        return 'queries/event_triggers/async_execution'

    def test_async_execution(self,hge_ctx,evts_webhook):
        """
        A test to check if the events generated by the graphql-engine are
        processed asynchronously. This test measures the time taken to process
        all the events and that time should definitely be lesser than the time
        taken if the events were to be executed sequentially.

        This test inserts 5 rows and the webhook(/sleep_2s) takes
        ~2 seconds to process one request. So, if the graphql-engine
        were to process the events sequentially it will take 5 * 2 = 10 seconds.
        Theorotically, all the events should have been processed in ~2 seconds,
        adding a 5 seconds buffer to the comparision, so that this test
        doesn't flake in the CI.
        """
        table = {"schema": "hge_tests", "name": "test_t1"}

        payload = range(1,6)
        rows = list(map(lambda x: {"c1": x, "c2": "hello"}, payload))
        st_code, resp = insert_many(hge_ctx, table, rows)
        start_time = time.perf_counter()
        assert st_code == 200, resp
        for i in range(1,6):
            _ = evts_webhook.get_event(5) # webhook takes 2 seconds to process a request (+ buffer)
        end_time = time.perf_counter()
        time_elapsed = end_time - start_time
        assert time_elapsed < 10
