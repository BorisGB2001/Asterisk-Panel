-- boris.gb 2026-10-31 14:35 - tables written by Asterisk itself (cdr_pgsql, cel_pgsql, queue_log via res_config_pgsql).
-- Owner: role "asterisk". The panel role only reads them. Times are UTC (timestamp without time zone).
-- Idempotent: safe to run again on every install/update.

CREATE TABLE IF NOT EXISTS cdr (
    id BIGSERIAL PRIMARY KEY,
    accountcode VARCHAR(80),
    src VARCHAR(80),
    dst VARCHAR(80),
    dcontext VARCHAR(80),
    clid VARCHAR(80),
    channel VARCHAR(80),
    dstchannel VARCHAR(80),
    lastapp VARCHAR(80),
    lastdata VARCHAR(255),
    start TIMESTAMP WITHOUT TIME ZONE,
    answer TIMESTAMP WITHOUT TIME ZONE,
    "end" TIMESTAMP WITHOUT TIME ZONE,
    duration INTEGER,
    billsec INTEGER,
    disposition VARCHAR(45),
    amaflags VARCHAR(45),
    userfield VARCHAR(256),
    uniqueid VARCHAR(150),
    linkedid VARCHAR(150),
    peeraccount VARCHAR(80),
    sequence INTEGER,
    -- custom CDR variable set in the dialplan: CDR(recordingfile)
    recordingfile VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS cdr_linkedid ON cdr (linkedid);
CREATE INDEX IF NOT EXISTS cdr_start ON cdr (start);

CREATE TABLE IF NOT EXISTS cel (
    id BIGSERIAL PRIMARY KEY,
    eventtype VARCHAR(30) NOT NULL,
    eventtime TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    userdeftype VARCHAR(255),
    cid_name VARCHAR(80),
    cid_num VARCHAR(80),
    cid_ani VARCHAR(80),
    cid_rdnis VARCHAR(80),
    cid_dnid VARCHAR(80),
    exten VARCHAR(80),
    context VARCHAR(80),
    channame VARCHAR(80),
    appname VARCHAR(80),
    appdata VARCHAR(512),
    amaflags INTEGER,
    accountcode VARCHAR(80),
    peeraccount VARCHAR(80),
    uniqueid VARCHAR(150),
    linkedid VARCHAR(150),
    userfield VARCHAR(255),
    peer VARCHAR(80),
    extra TEXT
);
CREATE INDEX IF NOT EXISTS cel_linkedid ON cel (linkedid);
CREATE INDEX IF NOT EXISTS cel_eventtime ON cel (eventtime);
CREATE INDEX IF NOT EXISTS cel_linkedid_end ON cel (eventtype) WHERE eventtype = 'LINKEDID_END';

CREATE TABLE IF NOT EXISTS queue_log (
    id BIGSERIAL PRIMARY KEY,
    time TIMESTAMP WITHOUT TIME ZONE,
    callid VARCHAR(80),
    queuename VARCHAR(256),
    agent VARCHAR(80),
    event VARCHAR(32),
    data1 VARCHAR(100),
    data2 VARCHAR(100),
    data3 VARCHAR(100),
    data4 VARCHAR(100),
    data5 VARCHAR(100)
);
-- no "data" column: with it Asterisk writes all fields into it joined by '|' instead of data1..data5
ALTER TABLE queue_log DROP COLUMN IF EXISTS data;
CREATE INDEX IF NOT EXISTS queue_log_callid ON queue_log (callid);
CREATE INDEX IF NOT EXISTS queue_log_time ON queue_log (time);
