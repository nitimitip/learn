/******************************************************************************
  V4-00 - POST-DEPLOY EVIDENCE - READ ONLY - PRODUCTION
  Nothing here changes data, schema or configuration.
  Run each block separately in QPID_PROD and save the grid as CSV
  (right-click > Save Results As), not as a screenshot.
  V4-00.5 is the only one that must run while the problem is happening.
******************************************************************************/
USE [QPID_PROD];
GO
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
GO

/* V4-00.1 Complete procedure stats, every procedure, one row per procedure.
   Replaces sp_execuation_stat.xlsx, which is missing a whole band of rows.
   Save as stats/2026-MM-DD_PRODUCTION_V4-00.1_procstats.csv */
SELECT  OBJECT_NAME(ps.object_id)                               AS ProcedureName,
        COUNT(*)                                                AS CacheEntries,
        SUM(ps.execution_count)                                 AS ExecutionCount,
        CAST(SUM(ps.total_elapsed_time) / 1000.0
             / NULLIF(SUM(ps.execution_count), 0) AS DECIMAL(18,2)) AS AvgDurationMs,
        CAST(SUM(ps.total_elapsed_time) / 1000.0 AS DECIMAL(18,2))  AS TotalDurationMs,
        CAST(MAX(ps.max_elapsed_time) / 1000.0 AS DECIMAL(18,2))    AS MaxDurationMs,
        CAST(SUM(ps.total_worker_time) / 1000.0
             / NULLIF(SUM(ps.execution_count), 0) AS DECIMAL(18,2)) AS AvgCpuMs,
        SUM(ps.total_logical_reads)
             / NULLIF(SUM(ps.execution_count), 0)               AS AvgLogicalReads,
        MAX(ps.max_logical_reads)                               AS MaxLogicalReads,
        MIN(ps.cached_time)                                     AS FirstCached,
        MAX(ps.cached_time)                                     AS LastCached,
        MAX(ps.last_execution_time)                             AS LastExecuted
FROM    sys.dm_exec_procedure_stats AS ps
WHERE   ps.database_id = DB_ID()
GROUP BY ps.object_id
ORDER BY TotalDurationMs DESC;
GO

/* V4-00.2a What is actually deployed in the suspect procedures.
   Expected if phase 2 is live:
     WildCardName     has_rowgoal_hint = 1
     MATCHINGCUST     recompile_hints  = 10  (only if V2-02 was deployed)
     InitialDataLoad  has_lock_hints   = 0   (1 = BF04 is live)
     sp_ACT_addHistory has_lock_hints  = 0   (1 = BF02 is live) */
SELECT  o.name, o.create_date, o.modify_date,
        LEN(m.definition) AS definition_len,
        CASE WHEN m.definition LIKE '%DISABLE_OPTIMIZER_ROWGOAL%' THEN 1 ELSE 0 END AS has_rowgoal_hint,
        (LEN(m.definition) - LEN(REPLACE(UPPER(m.definition), 'OPTION (RECOMPILE)', '')))
            / LEN('OPTION (RECOMPILE)')                                              AS recompile_hints,
        CASE WHEN m.definition LIKE '%UPDLOCK%' THEN 1 ELSE 0 END                    AS has_lock_hints,
        CASE WHEN m.definition LIKE '%sp_getapplock%' THEN 1 ELSE 0 END              AS has_applock
FROM    sys.objects AS o
JOIN    sys.sql_modules AS m ON m.object_id = o.object_id
WHERE   o.name IN (N'usp_GetActivityBasedOnTypeAndWildCardName',
                   N'usp_GetActivityBasedOnTypeAndName',
                   N'SP_ACT_GET_MATCHINGCUST',
                   N'SP_GetOldEnquiryNumber',
                   N'usp_QMS_InitialDataLoad_CRM',
                   N'SP_ADD_STANDARDS',
                   N'sp_ACT_addHistory',
                   N'usp_QMS_FinalDataLoad_CRM_SmallWork',
                   N'usp_QMS_FinalDataLoad_CRM_MajorWork',
                   N'PLA_addEnquiry',
                   N'PLA_addEnquiry_V2',
                   N'QPID_Perf_Capture_DB_Health')
ORDER BY o.name;
GO

/* V4-00.2b Every programmable object changed since the baseline. Shows what
   went live and when, including anything outside the V1/V2 package. */
SELECT  o.type_desc, o.name, o.create_date, o.modify_date
FROM    sys.objects AS o
WHERE   o.type IN ('P','FN','IF','TF','V','TR')
  AND   o.modify_date >= '20261012'
ORDER BY o.modify_date DESC;
GO

/* V4-00.2c Indexes on the tables in question, with key and INCLUDE columns.
   44-50 must show the deleted flag as INCLUDE; 51 on BT_ADDRESS_MASTER(postCode)
   was only a candidate: report whether it exists. */
SELECT  OBJECT_NAME(i.object_id) AS table_name, i.name AS index_name, i.type_desc,
        STUFF((SELECT ', ' + c.name + CASE WHEN ic.is_included_column = 1 THEN ' (INC)' ELSE '' END
               FROM   sys.index_columns AS ic
               JOIN   sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
               WHERE  ic.object_id = i.object_id AND ic.index_id = i.index_id
               ORDER BY ic.is_included_column, ic.key_ordinal, ic.index_column_id
               FOR XML PATH('')), 1, 2, '') AS columns_list
FROM    sys.indexes AS i
WHERE   i.object_id IN (OBJECT_ID(N'dbo.BT_Message'), OBJECT_ID(N'dbo.BT_ADDRESS_MASTER'),
                        OBJECT_ID(N'dbo.BT_CUSTOMER_MASTER'), OBJECT_ID(N'dbo.BT_CONTACTS_MASTER'),
                        OBJECT_ID(N'dbo.BT_ENQUIRY_MASTER'), OBJECT_ID(N'dbo.BT_QUOTE_MASTER'),
                        OBJECT_ID(N'dbo.BT_CTK_HEADER'), OBJECT_ID(N'dbo.BT_STK_HEADER'),
                        OBJECT_ID(N'dbo.BT_PROJ_U_HEADER'), OBJECT_ID(N'dbo.BT_PROJ_HEADER'),
                        OBJECT_ID(N'dbo.BT_ACT_BILLOFMATERIALS'))
ORDER BY table_name, i.index_id;
GO

/* V4-00.3 What the regressed procedures wait on, per day (Query Store).
   Shows WHICH wait (Lock = blocking) and WHEN it began, so it can be set
   against the deploy date. Empty result = Query Store off for this period. */
SELECT  OBJECT_NAME(q.object_id)                 AS proc_name,
        CAST(i.start_time AS DATE)               AS day_utc,
        ws.wait_category_desc,
        SUM(ws.total_query_wait_time_ms)         AS total_wait_ms,
        MAX(ws.max_query_wait_time_ms)           AS max_wait_ms
FROM    sys.query_store_wait_stats AS ws
JOIN    sys.query_store_plan AS p  ON p.plan_id = ws.plan_id
JOIN    sys.query_store_query AS q ON q.query_id = p.query_id
JOIN    sys.query_store_runtime_stats_interval AS i
        ON i.runtime_stats_interval_id = ws.runtime_stats_interval_id
WHERE   q.object_id IN (OBJECT_ID(N'dbo.SP_GetOldEnquiryNumber'),
                        OBJECT_ID(N'dbo.usp_QMS_InitialDataLoad_CRM'),
                        OBJECT_ID(N'dbo.SP_ADD_STANDARDS'))
  AND   i.start_time >= DATEADD(DAY, -30, SYSUTCDATETIME())
GROUP BY OBJECT_NAME(q.object_id), CAST(i.start_time AS DATE), ws.wait_category_desc
ORDER BY proc_name, day_utc, total_wait_ms DESC;
GO

/* V4-00.4a Daily history per plan (Query Store): was there a step change, and
   did a new plan cause it? */
SELECT  OBJECT_NAME(q.object_id)                           AS proc_name,
        CAST(i.start_time AS DATE)                         AS day_utc,
        q.query_id, p.plan_id,
        SUM(rs.count_executions)                           AS execs,
        CAST(SUM(rs.avg_duration * rs.count_executions) / 1000.0
             / NULLIF(SUM(rs.count_executions), 0) AS DECIMAL(18,2))   AS avg_ms,
        CAST(SUM(rs.avg_cpu_time * rs.count_executions) / 1000.0
             / NULLIF(SUM(rs.count_executions), 0) AS DECIMAL(18,2))   AS avg_cpu_ms,
        CAST(SUM(rs.avg_logical_io_reads * rs.count_executions)
             / NULLIF(SUM(rs.count_executions), 0) AS DECIMAL(18,0))   AS avg_reads,
        MAX(rs.max_duration) / 1000                        AS max_ms
FROM    sys.query_store_runtime_stats AS rs
JOIN    sys.query_store_runtime_stats_interval AS i
        ON i.runtime_stats_interval_id = rs.runtime_stats_interval_id
JOIN    sys.query_store_plan AS p  ON p.plan_id = rs.plan_id
JOIN    sys.query_store_query AS q ON q.query_id = p.query_id
WHERE   q.object_id IN (OBJECT_ID(N'dbo.SP_ACT_GET_MATCHINGCUST'),
                        OBJECT_ID(N'dbo.usp_GetActivityBasedOnTypeAndWildCardName'),
                        OBJECT_ID(N'dbo.SP_GetOldEnquiryNumber'))
  AND   i.start_time >= DATEADD(DAY, -30, SYSUTCDATETIME())
GROUP BY OBJECT_NAME(q.object_id), CAST(i.start_time AS DATE), q.query_id, p.plan_id
HAVING  SUM(rs.avg_logical_io_reads * rs.count_executions) > 0
ORDER BY proc_name, q.query_id, day_utc, p.plan_id;
GO

/* V4-00.4b Per-statement cost in the plan cache: which branch owns the reads
   and how many rows it returns. Needs the procedure to be in cache. */
SELECT  OBJECT_NAME(st.objectid, st.dbid)                  AS proc_name,
        qs.statement_start_offset / 2                      AS stmt_char_offset,
        qs.execution_count,
        qs.total_logical_reads / qs.execution_count        AS avg_reads,
        qs.max_logical_reads,
        qs.total_worker_time / qs.execution_count / 1000   AS avg_cpu_ms,
        qs.max_worker_time / 1000                          AS max_cpu_ms,
        qs.total_rows / qs.execution_count                 AS avg_rows,
        qs.max_rows,
        qs.creation_time,
        LEFT(SUBSTRING(st.text, qs.statement_start_offset / 2 + 1,
             (CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
              ELSE qs.statement_end_offset END - qs.statement_start_offset) / 2 + 1), 300) AS stmt_head
FROM    sys.dm_exec_query_stats AS qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) AS st
WHERE   st.dbid = DB_ID()
  AND   st.objectid IN (OBJECT_ID(N'dbo.SP_ACT_GET_MATCHINGCUST'),
                        OBJECT_ID(N'dbo.usp_GetActivityBasedOnTypeAndWildCardName'),
                        OBJECT_ID(N'dbo.usp_QMS_InitialDataLoad_CRM'))
ORDER BY proc_name, qs.total_logical_reads DESC;
GO

/* V4-00.5 Live blocking sample. Run it 5-10 times in business hours, a few
   seconds apart, ideally while CRM messages are loading. The head_blocker rows
   name the session holding everyone up; open_tran > 0 with a sleeping status
   means the application left a transaction open. */
SELECT  r.session_id, r.blocking_session_id, r.wait_type, r.wait_time AS wait_ms,
        r.wait_resource, OBJECT_NAME(st.objectid, st.dbid) AS waiting_proc,
        LEFT(st.text, 200) AS waiting_sql
FROM    sys.dm_exec_requests AS r
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) AS st
WHERE   r.blocking_session_id <> 0;

SELECT  'head_blocker' AS row_kind, s.session_id, s.status, s.open_transaction_count AS open_tran,
        s.program_name, s.host_name, s.login_name, s.last_request_end_time,
        OBJECT_NAME(st.objectid, st.dbid) AS last_proc, LEFT(st.text, 300) AS last_sql
FROM    sys.dm_exec_sessions AS s
LEFT JOIN sys.dm_exec_connections AS c ON c.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(c.most_recent_sql_handle) AS st
WHERE   s.session_id IN (SELECT r.blocking_session_id FROM sys.dm_exec_requests AS r
                         WHERE r.blocking_session_id <> 0)
  AND   s.session_id NOT IN (SELECT r.session_id FROM sys.dm_exec_requests AS r
                             WHERE r.blocking_session_id <> 0);
GO

/* V4-00.6 BT_Message facts for the candidate index design: lock waits since
   the last restart, forwarded records (why 2,078 reads vs 1,560 pages), and
   how selective the two reference columns are. */
SELECT  i.name AS index_name, os.index_id,
        os.row_lock_wait_count, os.row_lock_wait_in_ms,
        os.page_lock_wait_count, os.page_lock_wait_in_ms,
        os.page_latch_wait_count, os.page_latch_wait_in_ms,
        os.leaf_update_count, os.leaf_insert_count, os.range_scan_count, os.singleton_lookup_count
FROM    sys.dm_db_index_operational_stats(DB_ID(), OBJECT_ID(N'dbo.BT_Message'), NULL, NULL) AS os
LEFT JOIN sys.indexes AS i ON i.object_id = os.object_id AND i.index_id = os.index_id;

SELECT  index_id, page_count, record_count, forwarded_record_count, avg_page_space_used_in_percent
FROM    sys.dm_db_index_physical_stats(DB_ID(), OBJECT_ID(N'dbo.BT_Message'), NULL, NULL, 'DETAILED')
WHERE   index_level = 0;

SELECT  COUNT(*)                                                     AS total_rows,
        COUNT(DISTINCT QPID_Reference_Number)                        AS distinct_qpid_refs,
        COUNT(DISTINCT QMS_Reference_Number)                         AS distinct_qms_refs,
        SUM(CASE WHEN Message_Status = 'P' THEN 1 ELSE 0 END)        AS status_p,
        SUM(CASE WHEN Message_Status = 'W' THEN 1 ELSE 0 END)        AS status_w,
        SUM(CASE WHEN Message_Status = 'I' THEN 1 ELSE 0 END)        AS status_i,
        SUM(CASE WHEN Message_Status = 'F' THEN 1 ELSE 0 END)        AS status_f
FROM    dbo.BT_Message WITH (NOLOCK);
GO
