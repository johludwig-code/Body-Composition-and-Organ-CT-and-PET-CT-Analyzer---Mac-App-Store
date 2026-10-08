/// SQL the app runs on project.sqlite for the import (ADR 0020, ADR 0023,
/// ADR 0024): the merge of a catalog generation, the selection edits and the
/// identity edits.
///
/// Kept as text rather than built in Swift so that the Linux suite can execute
/// exactly these statements: `Worker/tests/test_merge_sql.py` extracts every
/// `static let … = """…"""` in this file by regex and runs it against catalogs
/// built with the worker's own DDL, so a change on either side fails there
/// before it reaches a Mac.
///
/// The statements take their inputs from temp tables, which each enum's
/// `…Inputs` or `inputs` creates and empties; `IndexStore` fills them and runs
/// the statements in one transaction. The texts must never contain a triple
/// quote or a backslash, which the extraction and Swift would read
/// differently.
public enum IndexSQL {
    /// `temp.merge_params`, one row: `now` (ISO 8601), `keep_identifiers`
    /// (0 after Remove Identifiers), `new_study_policy` (`refuse`, the default
    /// of open question 26, or `add_unlinked`) and the project's `link_key_id`
    /// (NULL after Remove Identifiers).
    public static let mergeInputs = """
        CREATE TEMP TABLE IF NOT EXISTS merge_params (
            now TEXT NOT NULL,
            keep_identifiers INTEGER NOT NULL CHECK (keep_identifiers IN (0, 1)),
            new_study_policy TEXT NOT NULL CHECK (new_study_policy IN ('refuse', 'add_unlinked')),
            link_key_id TEXT
        );
        DELETE FROM temp.merge_params;
        """

    /// Merges one complete catalog generation. Runs inside `BEGIN IMMEDIATE`
    /// with `index/catalog.sqlite` attached as `idx` (read-only where the
    /// system SQLite takes URI filenames; it writes nothing to `idx` either
    /// way, which the authorizer test holds), and is followed by
    /// `patientAges` in the same transaction. The guard in step 0 aborts the
    /// transaction for a catalog that is incomplete, of another format or
    /// already merged, and the caller rolls back.
    public static let merge = """
        -- 0. Refuse a catalog that is incomplete, of another format, or already merged.
        CREATE TEMP TABLE merge_guard (ok INTEGER NOT NULL CHECK (ok = 1));
        INSERT INTO temp.merge_guard SELECT
               (SELECT value FROM idx.catalog_meta WHERE key = 'format') = '1'
           AND (SELECT value FROM idx.catalog_meta WHERE key = 'complete') = '1'
           AND ((SELECT value FROM idx.catalog_meta WHERE key = 'catalog_id')
                    IS NOT (SELECT value FROM main.project_meta WHERE key = 'catalog_id')
                OR CAST((SELECT value FROM idx.catalog_meta WHERE key = 'generation') AS INTEGER)
                 > CAST((SELECT value FROM main.project_meta WHERE key = 'merged_generation') AS INTEGER));
        DROP TABLE temp.merge_guard;

        CREATE TEMP TABLE m_ctx AS SELECT
            (SELECT value FROM idx.catalog_meta WHERE key = 'catalog_id') AS catalog_id,
            p.now, p.keep_identifiers, p.new_study_policy,
            -- Links made with a key that is no longer the project's are worth nothing.
            ((SELECT value FROM idx.catalog_meta WHERE key = 'link_key_id') IS p.link_key_id
                AND p.link_key_id IS NOT NULL) AS links_valid
        FROM temp.merge_params AS p;

        -- 1. Known studies keep their patient and follow the catalog.
        UPDATE main.studies SET study_date = c.study_date, description = c.description,
            age_years = c.age_years, index_state = 'current'
        FROM idx.cat_studies AS c WHERE c.study_uid = main.studies.study_uid;
        UPDATE main.studies SET index_state = 'gone'
        WHERE study_uid NOT IN (SELECT study_uid FROM idx.cat_studies);

        -- 2. New studies find a patient: a PatientID link, then a confirmed folder link.
        CREATE TEMP TABLE m_new_studies AS
        SELECT c.study_uid, c.study_date, c.pid_state,
               CASE WHEN (SELECT links_valid FROM temp.m_ctx) THEN c.pid_link END AS pid_link,
               CASE WHEN (SELECT links_valid FROM temp.m_ctx) THEN coalesce(
                   (SELECT l.patient_key FROM main.patient_links AS l WHERE l.link = c.pid_link),
                   (SELECT l.patient_key FROM idx.cat_id_candidates AS k
                      JOIN main.patient_links AS l ON l.link = k.link
                     WHERE k.study_uid = c.study_uid ORDER BY k.level LIMIT 1)) END AS patient_key
        FROM idx.cat_studies AS c
        WHERE c.study_uid NOT IN (SELECT study_uid FROM main.studies);

        -- After Remove Identifiers no new study can be matched to a patient; by
        -- default it is not added at all (owner question), only counted.
        CREATE TEMP TABLE m_refused AS
        SELECT study_uid FROM temp.m_new_studies
        WHERE patient_key IS NULL AND (SELECT new_study_policy FROM temp.m_ctx) = 'refuse'
          AND (SELECT keep_identifiers FROM temp.m_ctx) = 0;
        DELETE FROM temp.m_new_studies WHERE study_uid IN (SELECT study_uid FROM temp.m_refused);

        -- 3. One new patient per unknown link, or per study without one.
        CREATE TEMP TABLE m_new_patients AS
        SELECT grp, pid_link, pid_state, has_candidates,
               row_number() OVER (ORDER BY first_date, first_uid) AS n
        FROM (SELECT coalesce(s.pid_link, 'study:' || s.study_uid) AS grp, s.pid_link,
                     min(s.pid_state) AS pid_state,
                     max(EXISTS (SELECT 1 FROM idx.cat_id_candidates AS k WHERE k.study_uid = s.study_uid))
                         AS has_candidates,
                     min(coalesce(s.study_date, '9999-12-31')) AS first_date, min(s.study_uid) AS first_uid
                FROM temp.m_new_studies AS s WHERE s.patient_key IS NULL GROUP BY 1);

        -- Held as unconfirmed only where the catalog offers a folder ID to confirm
        -- (ADR 0024). Without one (folder IDs switched off) the sheet would have
        -- nothing to propose and every study would wait for an ID typed by hand,
        -- so the study becomes a patient of its own and is selected automatically.
        INSERT INTO main.patients (patient_key, pseudonym, id_status)
        SELECT printf('pt_%06d', (SELECT last FROM main.key_counters WHERE kind = 'patient') + n),
               printf('P%04d', (SELECT last FROM main.key_counters WHERE kind = 'pseudonym') + n),
               CASE WHEN NOT (SELECT links_valid FROM temp.m_ctx) THEN 'unlinked'
                    WHEN pid_state = 'present' THEN 'dicom'
                    WHEN pid_state = 'file' THEN 'file'
                    WHEN pid_state = 'withheld' THEN 'unlinked'
                    WHEN has_candidates THEN 'unconfirmed'
                    ELSE 'unlinked' END
        FROM temp.m_new_patients ORDER BY n;

        UPDATE temp.m_new_studies SET patient_key = (
            SELECT printf('pt_%06d', (SELECT last FROM main.key_counters WHERE kind = 'patient') + p.n)
              FROM temp.m_new_patients AS p
             WHERE p.grp = coalesce(temp.m_new_studies.pid_link, 'study:' || temp.m_new_studies.study_uid))
        WHERE patient_key IS NULL;

        INSERT INTO main.patient_links (link, patient_key)
        SELECT pid_link, min(patient_key) FROM temp.m_new_studies
        WHERE pid_link IS NOT NULL AND pid_link NOT IN (SELECT link FROM main.patient_links)
        GROUP BY pid_link;

        UPDATE main.key_counters SET last = last + (SELECT count(*) FROM temp.m_new_patients)
        WHERE kind IN ('patient', 'pseudonym');

        -- 4. New studies get keys in date order.
        INSERT INTO main.studies (study_key, patient_key, study_uid, study_date, description, age_years)
        SELECT printf('st_%06d', (SELECT last FROM main.key_counters WHERE kind = 'study')
                   + row_number() OVER (ORDER BY coalesce(c.study_date, '9999-12-31'), c.study_uid)),
               n.patient_key, c.study_uid, c.study_date, c.description, c.age_years
        FROM temp.m_new_studies AS n JOIN idx.cat_studies AS c USING (study_uid);
        UPDATE main.key_counters SET last = last + (SELECT count(*) FROM temp.m_new_studies)
        WHERE kind = 'study';

        -- 5. Series: map catalog parts to series keys. First by the catalog's own
        -- part identity (the worker carries it across regroups by instance overlap),
        -- then by series UID and fingerprint (files that came back, a rebuilt catalog).
        -- Only parts whose study is in the project are mapped: a known series whose
        -- files came back under a refused new study (identifiers removed) would
        -- otherwise be moved to a study that does not exist, and the merge would
        -- abort on every later scan. Unmapped, it simply becomes gone.
        CREATE TEMP TABLE m_map (part_ref INTEGER PRIMARY KEY, series_key TEXT NOT NULL UNIQUE);
        INSERT INTO temp.m_map
        SELECT c.part_ref, s.series_key FROM idx.cat_series AS c
        JOIN main.series AS s ON s.catalog_part = (SELECT catalog_id FROM temp.m_ctx) || ':' || c.part_ref
        WHERE c.study_uid IN (SELECT study_uid FROM main.studies);
        INSERT OR IGNORE INTO temp.m_map
        SELECT c.part_ref, s.series_key FROM idx.cat_series AS c
        JOIN main.series AS s ON s.series_uid = c.series_uid AND s.fingerprint = c.fingerprint
        WHERE c.part_ref NOT IN (SELECT part_ref FROM temp.m_map)
          AND s.series_key NOT IN (SELECT series_key FROM temp.m_map)
          AND c.study_uid IN (SELECT study_uid FROM main.studies);

        -- A mapped series whose files now name another study (a study merge in the
        -- PACS keeps series and instance UIDs) moves there without its primary flag:
        -- a second primary in the study it joins would abort the merge at the unique
        -- index, on every later scan.
        CREATE TEMP TABLE m_moved AS
        SELECT s.series_key FROM temp.m_map AS m
        JOIN idx.cat_series AS c USING (part_ref)
        JOIN main.series AS s ON s.series_key = m.series_key
        JOIN main.studies AS st ON st.study_uid = c.study_uid
        WHERE st.study_key <> s.study_key;

        -- Studies whose primary is about to disappear, or to move away, return to
        -- automatic selection.
        CREATE TEMP TABLE m_lost_primary AS
        SELECT study_key FROM main.series
        WHERE is_primary = 1
          AND (series_key NOT IN (SELECT series_key FROM temp.m_map)
               OR series_key IN (SELECT series_key FROM temp.m_moved));
        UPDATE main.series SET is_primary = 0
        WHERE is_primary = 1 AND series_key IN (SELECT series_key FROM temp.m_moved);

        UPDATE main.series SET index_state = 'gone', selected = 0, is_primary = 0, auto_selected = 0,
            auto_rank = NULL, catalog_part = NULL, part = -rowid
        WHERE series_key NOT IN (SELECT series_key FROM temp.m_map) AND index_state = 'current';

        -- Parts may be renumbered; move them out of the way of UNIQUE (series_uid, part).
        UPDATE main.series SET part = -rowid WHERE series_key IN (SELECT series_key FROM temp.m_map);
        UPDATE main.series SET
            study_key = (SELECT st.study_key FROM main.studies AS st WHERE st.study_uid = c.study_uid),
            series_uid = c.series_uid, part = c.part,
            catalog_part = (SELECT catalog_id FROM temp.m_ctx) || ':' || c.part_ref,
            modality = c.modality, description = c.description, image_count = c.image_count,
            slice_thickness_mm = c.slice_thickness_mm, pixel_spacing_mm = c.pixel_spacing_mm,
            kernel = c.kernel, kernel_class = c.kernel_class, manufacturer = c.manufacturer,
            kvp = c.kvp, contrast_agent = c.contrast_agent, image_type = c.image_type,
            frame_of_reference_uid = c.frame_of_reference_uid, fingerprint = c.fingerprint,
            series_number = c.series_number, sop_class_uid = c.sop_class_uid,
            scanner_model = c.scanner_model, slice_count = c.slice_count,
            slice_spacing_mm = c.slice_spacing_mm, z_extent_mm = c.z_extent_mm,
            orientation = c.orientation, image_rows = c.image_rows, image_columns = c.image_columns,
            transfer_syntax_uid = c.transfer_syntax_uid, auto_rank = c.auto_rank,
            auto_selected = c.auto_selected, selection_reason = c.reason_json, index_state = 'current'
        FROM temp.m_map AS m JOIN idx.cat_series AS c ON c.part_ref = m.part_ref
        WHERE main.series.series_key = m.series_key;

        -- A study chosen by hand keeps exactly the choice made in it: a series that
        -- moves in arrives unselected, as a new series does, and is counted in
        -- check.new_series_since_manual. Still selected, it would stand without a
        -- primary in a study the user had deselected entirely (step 7 never
        -- touches a study in user mode), and the export would take it.
        UPDATE main.series SET selected = 0, selection_origin = 'auto', selection_cohort_id = NULL
        WHERE series_key IN (SELECT series_key FROM temp.m_moved)
          AND study_key IN (SELECT study_key FROM main.studies WHERE selection_mode = 'user');

        CREATE TEMP TABLE m_new_series AS
        SELECT c.part_ref, st.study_key,
               row_number() OVER (ORDER BY st.study_key, c.series_number, c.series_uid, c.part) AS n
        FROM idx.cat_series AS c JOIN main.studies AS st ON st.study_uid = c.study_uid
        WHERE c.part_ref NOT IN (SELECT part_ref FROM temp.m_map);

        INSERT INTO main.series (series_key, study_key, series_uid, part, catalog_part, modality,
            description, image_count, slice_thickness_mm, pixel_spacing_mm, kernel, kernel_class,
            manufacturer, kvp, contrast_agent, image_type, frame_of_reference_uid, fingerprint,
            series_number, sop_class_uid, scanner_model, slice_count, slice_spacing_mm, z_extent_mm,
            orientation, image_rows, image_columns, transfer_syntax_uid, auto_rank, auto_selected,
            selection_reason)
        SELECT printf('s_%06d', (SELECT last FROM main.key_counters WHERE kind = 'series') + n.n),
               n.study_key, c.series_uid, c.part, (SELECT catalog_id FROM temp.m_ctx) || ':' || c.part_ref,
               c.modality, c.description, c.image_count, c.slice_thickness_mm, c.pixel_spacing_mm,
               c.kernel, c.kernel_class, c.manufacturer, c.kvp, c.contrast_agent, c.image_type,
               c.frame_of_reference_uid, c.fingerprint, c.series_number, c.sop_class_uid,
               c.scanner_model, c.slice_count, c.slice_spacing_mm, c.z_extent_mm, c.orientation,
               c.image_rows, c.image_columns, c.transfer_syntax_uid, c.auto_rank, c.auto_selected,
               c.reason_json
        FROM temp.m_new_series AS n JOIN idx.cat_series AS c USING (part_ref);
        UPDATE main.key_counters SET last = last + (SELECT count(*) FROM temp.m_new_series)
        WHERE kind = 'series';

        -- 6. What nothing refers to any more is deleted; what results, QC, jobs or a
        -- cohort refer to stays, marked gone. Pseudonyms and keys are never reused.
        DELETE FROM main.series WHERE index_state = 'gone'
          AND series_key NOT IN (SELECT series_key FROM main.results
                                 UNION SELECT series_key FROM main.qc
                                 UNION SELECT series_key FROM main.jobs WHERE series_key IS NOT NULL
                                 UNION SELECT series_key FROM main.cohort_series);
        DELETE FROM main.studies WHERE index_state = 'gone'
          AND study_key NOT IN (SELECT study_key FROM main.series);
        DELETE FROM main.patients WHERE patient_key NOT IN (SELECT patient_key FROM main.studies);

        -- 7. Selection. A primary is cleared before another is set (the unique index
        -- is checked row by row), and set only after `selected` (the trigger).
        UPDATE main.studies SET selection_mode = 'auto'
        WHERE selection_mode = 'user' AND study_key IN (SELECT study_key FROM temp.m_lost_primary);
        UPDATE main.series SET is_primary = 0
        WHERE is_primary = 1
          AND study_key IN (SELECT study_key FROM main.studies WHERE selection_mode = 'auto');
        UPDATE main.series SET selected = auto_selected, selection_origin = 'auto', selection_cohort_id = NULL
        WHERE index_state = 'current'
          AND study_key IN (SELECT study_key FROM main.studies WHERE selection_mode = 'auto');
        UPDATE main.series SET is_primary = 1
        WHERE auto_selected = 1 AND index_state = 'current'
          AND study_key IN (SELECT study_key FROM main.studies WHERE selection_mode = 'auto');
        -- A patient whose ID came from a folder name and is not confirmed is proposed, never selected.
        UPDATE main.series SET is_primary = 0, selected = 0
        WHERE (selected = 1 OR is_primary = 1)
          AND study_key IN (SELECT st.study_key FROM main.studies AS st JOIN main.patients AS p USING (patient_key)
                             WHERE p.id_status = 'unconfirmed');

        -- 8. Identifiers, unless they were removed for good.
        INSERT INTO main.identifiers (patient_key, patient_id, accession_numbers, id_source)
        SELECT st.patient_key, min(i.patient_id), NULL, min(i.id_source)
        FROM idx.pending_identifiers AS i JOIN main.studies AS st USING (study_uid)
        JOIN main.patients AS p ON p.patient_key = st.patient_key
        WHERE (SELECT keep_identifiers FROM temp.m_ctx) = 1 AND i.patient_id IS NOT NULL
          AND p.id_status IN ('dicom', 'file', 'confirmed')
          AND st.patient_key NOT IN (SELECT patient_key FROM main.identifiers)
        GROUP BY st.patient_key;
        UPDATE main.identifiers SET accession_numbers = (
            SELECT json_group_array(value) FROM (
                SELECT value FROM json_each(coalesce(main.identifiers.accession_numbers, '[]'))
                UNION
                SELECT i.accession_number FROM idx.pending_identifiers AS i
                  JOIN main.studies AS st USING (study_uid)
                 WHERE st.patient_key = main.identifiers.patient_key
                   AND coalesce(i.accession_number, '') <> ''
                ORDER BY 1))
        WHERE (SELECT keep_identifiers FROM temp.m_ctx) = 1
          AND patient_key IN (SELECT st.patient_key FROM idx.pending_identifiers
                                JOIN main.studies AS st USING (study_uid));

        -- 9. Patients: sex only if every study that records one agrees. The age
        -- follows in IndexSQL.patientAges, once the selection is final.
        UPDATE main.patients SET sex = (
            SELECT CASE WHEN count(DISTINCT c.sex) = 1 THEN min(c.sex) END
              FROM main.studies AS st JOIN idx.cat_studies AS c USING (study_uid)
             WHERE st.patient_key = main.patients.patient_key AND c.sex IS NOT NULL);

        -- 10. Checks and pairs are rebuilt from the catalog, plus what only the project knows.
        DELETE FROM main.index_checks;
        INSERT INTO main.index_checks (object_kind, object_key, code, level, params_json)
        SELECT 'source', k.object_ref, k.code, k.level, k.params_json
          FROM idx.cat_checks AS k WHERE k.object_kind = 'source'
        UNION ALL
        SELECT 'study', st.study_key, k.code, k.level, k.params_json
          FROM idx.cat_checks AS k JOIN main.studies AS st ON st.study_uid = k.object_ref
         WHERE k.object_kind = 'study'
        UNION ALL
        SELECT 'series', m.series_key, k.code, k.level, k.params_json
          FROM idx.cat_checks AS k JOIN temp.m_map AS m ON CAST(k.object_ref AS INTEGER) = m.part_ref
         WHERE k.object_kind = 'series'
        UNION ALL
        SELECT 'series', s.series_key, k.code, k.level, k.params_json
          FROM idx.cat_checks AS k
          JOIN main.series AS s ON s.catalog_part = (SELECT catalog_id FROM temp.m_ctx) || ':' || k.object_ref
         WHERE k.object_kind = 'series' AND CAST(k.object_ref AS INTEGER) NOT IN (SELECT part_ref FROM temp.m_map);
        INSERT INTO main.index_checks (object_kind, object_key, code, level, params_json)
        SELECT 'patient', st.patient_key, 'check.sex_conflict', 'warning',
               json_object('values', group_concat(DISTINCT c.sex))
          FROM main.studies AS st JOIN idx.cat_studies AS c USING (study_uid)
         WHERE c.sex IS NOT NULL GROUP BY st.patient_key HAVING count(DISTINCT c.sex) > 1;
        INSERT INTO main.index_checks (object_kind, object_key, code, level, params_json)
        SELECT 'study', st.study_key, 'check.new_series_since_manual', 'info',
               json_object('count', count(*), 'would_be_chosen', max(s.auto_selected))
          FROM (SELECT s.series_key FROM temp.m_new_series AS n JOIN main.series AS s
                  ON s.catalog_part = (SELECT catalog_id FROM temp.m_ctx) || ':' || n.part_ref
                UNION
                SELECT series_key FROM temp.m_moved) AS arrived
          JOIN main.series AS s USING (series_key)
          JOIN main.studies AS st ON st.study_key = s.study_key
         WHERE st.selection_mode = 'user' AND s.auto_rank IS NOT NULL
         GROUP BY st.study_key;
        INSERT INTO main.index_checks (object_kind, object_key, code, level, params_json)
        SELECT 'study', study_key, 'check.primary_gone', 'warning', '{}'
          FROM temp.m_lost_primary WHERE study_key IN (SELECT study_key FROM main.studies);
        INSERT INTO main.index_checks (object_kind, object_key, code, level, params_json)
        SELECT 'project', 'project', 'check.new_studies_not_added', 'warning',
               json_object('count', count(*))
          FROM temp.m_refused HAVING count(*) > 0;

        DELETE FROM main.series_pairs;
        INSERT INTO main.series_pairs (pet_series_key, ct_series_key, pet_attenuation_corrected, z_overlap_mm)
        SELECT pt.series_key, ct.series_key, p.pet_attenuation_corrected, p.z_overlap_mm
        FROM idx.cat_pairs AS p
        JOIN main.series AS pt ON pt.catalog_part = (SELECT catalog_id FROM temp.m_ctx) || ':' || p.pet_part_ref
        JOIN main.series AS ct ON ct.catalog_part = (SELECT catalog_id FROM temp.m_ctx) || ':' || p.ct_part_ref;

        UPDATE main.sources SET
            state = CASE s.state WHEN 'complete' THEN 'indexed' WHEN 'unreachable' THEN 'unreachable'
                                 ELSE 'interrupted' END,
            indexed_at = coalesce(s.finished_at, main.sources.indexed_at),
            summary_json = s.summary_json
        FROM idx.scans AS s WHERE s.source_id = main.sources.id AND main.sources.state <> 'removed';

        INSERT OR REPLACE INTO main.project_meta (key, value)
        SELECT 'merged_generation', value FROM idx.catalog_meta WHERE key = 'generation';
        INSERT OR REPLACE INTO main.project_meta (key, value)
        SELECT 'catalog_id', catalog_id FROM temp.m_ctx;

        INSERT INTO main.audit_log (at, actor, action, object, details_json)
        SELECT now, 'app', 'index_merged', 'index',
               json_object('generation',
                           CAST((SELECT value FROM idx.catalog_meta WHERE key = 'generation') AS INTEGER),
                           'new_patients', (SELECT count(*) FROM temp.m_new_patients),
                           'new_studies', (SELECT count(*) FROM temp.m_new_studies),
                           'new_series', (SELECT count(*) FROM temp.m_new_series),
                           'refused_studies', (SELECT count(*) FROM temp.m_refused),
                           'gone_series', (SELECT count(*) FROM main.series WHERE index_state = 'gone'))
        FROM temp.m_ctx;

        DROP TABLE temp.m_ctx; DROP TABLE temp.m_new_studies; DROP TABLE temp.m_refused;
        DROP TABLE temp.m_new_patients; DROP TABLE temp.m_map; DROP TABLE temp.m_lost_primary;
        DROP TABLE temp.m_new_series; DROP TABLE temp.m_moved;
        """

    /// After every merge and every selection or identity edit: the age at the
    /// export's first time point, which is the first study with a selected
    /// series, else the first dated study (open question 27); and the check
    /// for ages that fit no single birth date.
    public static let patientAges = """
        UPDATE main.patients SET age_at_first_study = (
            SELECT st.age_years FROM main.studies AS st
             WHERE st.patient_key = main.patients.patient_key AND st.index_state = 'current'
             ORDER BY NOT EXISTS (SELECT 1 FROM main.series AS s
                                   WHERE s.study_key = st.study_key AND s.selected = 1),
                      coalesce(st.study_date, '9999-12-31'), st.study_key
             LIMIT 1);
        DELETE FROM main.index_checks WHERE object_kind = 'patient' AND code = 'check.age_inconsistent';
        INSERT INTO main.index_checks (object_kind, object_key, code, level, params_json)
        SELECT 'patient', patient_key, 'check.age_inconsistent', 'warning', json_object('studies', count(*))
        FROM (SELECT st.patient_key, julianday(st.study_date) - st.age_years * 365.2425 AS birth
                FROM main.studies AS st
               WHERE st.age_years IS NOT NULL AND st.study_date IS NOT NULL AND st.index_state = 'current')
        GROUP BY patient_key HAVING max(birth) - min(birth) > 1.5 * 365.2425;
        """
}

/// Selection edits (ADR 0023). Each edit runs in one transaction: the
/// statement, then `repairPrimary`, then `IndexSQL.patientAges`; `IndexStore`
/// adds the audit row `selection_changed` with counts only. A primary is
/// always cleared before another is set and set only after `selected`,
/// because the unique index is checked row by row and the triggers refuse a
/// primary that is not selected.
public enum SelectionSQL {
    /// `temp.sel_target` (series keys), `temp.sel_scope` (study keys) and
    /// `temp.sel_param`, one row: `max_mm`, `label`, `now`, `cohort_id`, each
    /// used by the statements that name it.
    public static let inputs = """
        CREATE TEMP TABLE IF NOT EXISTS sel_target (series_key TEXT PRIMARY KEY);
        CREATE TEMP TABLE IF NOT EXISTS sel_scope (study_key TEXT PRIMARY KEY);
        CREATE TEMP TABLE IF NOT EXISTS sel_param (max_mm REAL, label TEXT, now TEXT, cohort_id INTEGER);
        DELETE FROM temp.sel_target;
        DELETE FROM temp.sel_scope;
        DELETE FROM temp.sel_param;
        """

    /// "Make Primary": one series in `sel_target`.
    public static let makePrimary = """
        UPDATE main.studies SET selection_mode = 'user'
        WHERE study_key IN (SELECT s.study_key FROM main.series AS s JOIN temp.sel_target USING (series_key));
        UPDATE main.series SET is_primary = 0
        WHERE is_primary = 1
          AND study_key IN (SELECT s.study_key FROM main.series AS s JOIN temp.sel_target USING (series_key));
        UPDATE main.series SET selected = 1, is_primary = 1, selection_origin = 'user', selection_cohort_id = NULL
        WHERE series_key IN (SELECT series_key FROM temp.sel_target) AND index_state = 'current'
          AND study_key NOT IN (SELECT st.study_key FROM main.studies AS st JOIN main.patients AS p USING (patient_key)
                                 WHERE p.id_status = 'unconfirmed');
        """

    /// "Select": the series in `sel_target`, without changing the primary.
    public static let select = """
        UPDATE main.studies SET selection_mode = 'user'
        WHERE study_key IN (SELECT s.study_key FROM main.series AS s JOIN temp.sel_target USING (series_key));
        UPDATE main.series SET selected = 1, selection_origin = 'user', selection_cohort_id = NULL
        WHERE series_key IN (SELECT series_key FROM temp.sel_target) AND index_state = 'current'
          AND study_key NOT IN (SELECT st.study_key FROM main.studies AS st JOIN main.patients AS p USING (patient_key)
                                 WHERE p.id_status = 'unconfirmed');
        """

    /// "Deselect": the series in `sel_target`.
    public static let deselect = """
        UPDATE main.studies SET selection_mode = 'user'
        WHERE study_key IN (SELECT s.study_key FROM main.series AS s JOIN temp.sel_target USING (series_key));
        UPDATE main.series SET is_primary = 0, selected = 0, selection_origin = 'user', selection_cohort_id = NULL
        WHERE series_key IN (SELECT series_key FROM temp.sel_target);
        """

    /// "Select All CT ≤ max mm…": adds eligible CT (those with an
    /// `auto_rank`) of the studies in `sel_scope`, with 0.02 mm of tolerance;
    /// never removes a selection or changes a primary. `sel_param.max_mm`.
    public static let bulkThinCT = """
        CREATE TEMP TABLE m_bulk AS
        SELECT s.series_key, s.study_key FROM main.series AS s
        JOIN temp.sel_scope USING (study_key)
        JOIN main.studies AS st ON st.study_key = s.study_key
        JOIN main.patients AS p ON p.patient_key = st.patient_key
        WHERE s.index_state = 'current' AND s.modality = 'CT' AND s.auto_rank IS NOT NULL
          AND s.selected = 0 AND p.id_status <> 'unconfirmed'
          AND s.slice_thickness_mm <= (SELECT max_mm FROM temp.sel_param) + 0.02;
        UPDATE main.studies SET selection_mode = 'user' WHERE study_key IN (SELECT study_key FROM temp.m_bulk);
        UPDATE main.series SET selected = 1, selection_origin = 'bulk_thin_ct', selection_cohort_id = NULL
        WHERE series_key IN (SELECT series_key FROM temp.m_bulk);
        DROP TABLE temp.m_bulk;
        """

    /// "Apply Auto-Selection to All", and after a confirmation: the studies in
    /// `sel_scope` return to the stored automatic choice.
    public static let applyAuto = """
        UPDATE main.studies SET selection_mode = 'auto' WHERE study_key IN (SELECT study_key FROM temp.sel_scope);
        UPDATE main.series SET is_primary = 0
        WHERE is_primary = 1 AND study_key IN (SELECT study_key FROM temp.sel_scope);
        UPDATE main.series SET selected = auto_selected, selection_origin = 'auto', selection_cohort_id = NULL
        WHERE index_state = 'current' AND study_key IN (SELECT study_key FROM temp.sel_scope)
          AND study_key NOT IN (SELECT st.study_key FROM main.studies AS st JOIN main.patients AS p USING (patient_key)
                                 WHERE p.id_status = 'unconfirmed');
        UPDATE main.series SET is_primary = 1
        WHERE auto_selected = 1 AND selected = 1 AND study_key IN (SELECT study_key FROM temp.sel_scope);
        """

    /// After every edit: a study in `sel_scope` with a selected series but no
    /// primary gets the best-ranked selected series as its primary.
    public static let repairPrimary = """
        UPDATE main.series SET is_primary = 1
        WHERE series_key IN (
            SELECT series_key FROM (
                SELECT series_key, row_number() OVER (PARTITION BY study_key
                    ORDER BY auto_rank IS NULL, auto_rank, series_number IS NULL, series_number, series_key) AS n
                FROM main.series WHERE selected = 1)
            WHERE n = 1)
          AND study_key NOT IN (SELECT study_key FROM main.series WHERE is_primary = 1)
          AND study_key IN (SELECT study_key FROM temp.sel_scope);
        """

    /// "Save Selection as Cohort…": `sel_param.label` (unique) and `now`.
    public static let saveCohort = """
        INSERT INTO main.cohorts (label, created_at) SELECT label, now FROM temp.sel_param;
        INSERT INTO main.cohort_series (cohort_id, series_key, is_primary)
        SELECT (SELECT c.cohort_id FROM main.cohorts AS c WHERE c.label = (SELECT label FROM temp.sel_param)),
               series_key, is_primary
        FROM main.series WHERE selected = 1 AND index_state = 'current';
        """

    /// "Apply Cohort": replaces the whole selection. `sel_param.cohort_id`;
    /// `sel_scope` holds every current study.
    public static let applyCohort = """
        UPDATE main.studies SET selection_mode = 'user' WHERE study_key IN (SELECT study_key FROM temp.sel_scope);
        UPDATE main.series SET is_primary = 0
        WHERE is_primary = 1 AND study_key IN (SELECT study_key FROM temp.sel_scope);
        UPDATE main.series SET
            selected = (series_key IN (SELECT cs.series_key FROM main.cohort_series AS cs
                                        WHERE cs.cohort_id = (SELECT cohort_id FROM temp.sel_param))),
            selection_origin = 'cohort', selection_cohort_id = (SELECT cohort_id FROM temp.sel_param)
        WHERE index_state = 'current' AND study_key IN (SELECT study_key FROM temp.sel_scope)
          AND study_key NOT IN (SELECT st.study_key FROM main.studies AS st JOIN main.patients AS p USING (patient_key)
                                 WHERE p.id_status = 'unconfirmed');
        -- A cohort holds one primary per study when it is saved, but a series that
        -- has since moved into another study (a study merge in the PACS) brings its
        -- flag along: such a study takes the one @repairPrimary would pick, instead of
        -- two primaries aborting the whole statement at the unique index.
        UPDATE main.series SET is_primary = 1
        WHERE series_key IN (
            SELECT series_key FROM (
                SELECT s.series_key, row_number() OVER (PARTITION BY s.study_key
                    ORDER BY s.auto_rank IS NULL, s.auto_rank, s.series_number IS NULL, s.series_number,
                             s.series_key) AS n
                FROM main.series AS s JOIN main.cohort_series AS cs USING (series_key)
                WHERE cs.cohort_id = (SELECT cohort_id FROM temp.sel_param) AND cs.is_primary = 1
                  AND s.selected = 1 AND s.study_key IN (SELECT study_key FROM temp.sel_scope))
            WHERE n = 1);
        """
}

/// Identity edits (ADR 0024): confirming folder IDs, assigning a study to a
/// patient, a patient for a typed ID. Each runs in one transaction followed by
/// `SelectionSQL.applyAuto`, `SelectionSQL.repairPrimary` and
/// `IndexSQL.patientAges` on the `sel_scope` it leaves, so the caller runs
/// `SelectionSQL.inputs` as well. Patients are merged, never renumbered: keys
/// and pseudonyms of emptied patients are not reused.
public enum IdentitySQL {
    /// `temp.id_param`, one row: `now`, `actor`, `keep_identifiers`, and the
    /// folder `level` confirmed (NULL for typed IDs only). `temp.id_confirm`:
    /// per study the chosen `link` (`folder:` or `pid:`), its `label` (the
    /// folder name or the typed ID) and `id_source` (`folder` or `typed`).
    /// `temp.id_assign`: study → patient. `temp.id_typed`: per study the
    /// `link` and the typed `patient_id`. Labels and typed IDs are plain
    /// identifiers, which is why the statements that read them empty them.
    public static let inputs = """
        CREATE TEMP TABLE IF NOT EXISTS id_param (
            now TEXT NOT NULL,
            actor TEXT NOT NULL,
            keep_identifiers INTEGER NOT NULL CHECK (keep_identifiers IN (0, 1)),
            level INTEGER
        );
        CREATE TEMP TABLE IF NOT EXISTS id_confirm (
            study_key TEXT PRIMARY KEY,
            link TEXT NOT NULL,
            label TEXT NOT NULL,
            id_source TEXT NOT NULL CHECK (id_source IN ('folder', 'typed'))
        );
        CREATE TEMP TABLE IF NOT EXISTS id_assign (study_key TEXT PRIMARY KEY, patient_key TEXT NOT NULL);
        CREATE TEMP TABLE IF NOT EXISTS id_typed (
            study_key TEXT PRIMARY KEY,
            link TEXT NOT NULL,
            patient_id TEXT NOT NULL
        );
        DELETE FROM temp.id_param;
        DELETE FROM temp.id_confirm;
        DELETE FROM temp.id_assign;
        DELETE FROM temp.id_typed;
        """

    /// "Confirm Patient IDs…": every study in `id_confirm` joins the patient
    /// that holds its link, or else the lowest patient key of its link's
    /// group, which becomes `confirmed` and gets the link; the identifiers row
    /// is written only while identifiers are kept. Leaves the confirmed
    /// studies in `sel_scope` and writes the audit row `ids_confirmed`.
    public static let confirmFolderLevel = """
        -- The sheet lists unconfirmed patients only. A row whose patient was
        -- confirmed or assigned since the sheet was read is left out, not moved.
        CREATE TEMP TABLE i_rows AS
        SELECT c.study_key, c.link, c.label, c.id_source, st.patient_key AS old_patient
        FROM temp.id_confirm AS c
        JOIN main.studies AS st USING (study_key)
        JOIN main.patients AS p ON p.patient_key = st.patient_key
        WHERE p.id_status = 'unconfirmed';

        -- Each link goes to the patient that already holds it, else to the lowest
        -- patient key of its group: key order, because 'P10000' sorts before 'P9999'
        -- as text and pseudonym order would not be the order of creation.
        CREATE TEMP TABLE i_groups AS
        SELECT r.link,
               coalesce((SELECT l.patient_key FROM main.patient_links AS l WHERE l.link = r.link),
                        min(r.old_patient)) AS target,
               min(r.label) AS label, min(r.id_source) AS id_source
        FROM temp.i_rows AS r GROUP BY r.link;

        CREATE TEMP TABLE i_members AS
        SELECT target, target AS patient_key FROM temp.i_groups
        UNION
        SELECT g.target, r.old_patient FROM temp.i_rows AS r JOIN temp.i_groups AS g USING (link);
        -- Sex is kept only if every patient joined into a target agrees, as the
        -- merge keeps it only if every study agrees; a patient whose studies already
        -- disagree brings the values its check.sex_conflict lists. The next merge
        -- recomputes both from the studies themselves.
        CREATE TEMP TABLE i_sexes AS
        SELECT m.target, p.sex FROM temp.i_members AS m
        JOIN main.patients AS p ON p.patient_key = m.patient_key WHERE p.sex IS NOT NULL
        UNION
        SELECT m.target, v.sex FROM temp.i_members AS m
        JOIN main.index_checks AS k
          ON k.object_kind = 'patient' AND k.object_key = m.patient_key AND k.code = 'check.sex_conflict'
        JOIN (SELECT 'F' AS sex UNION ALL SELECT 'M' UNION ALL SELECT 'O') AS v
          ON instr(json_extract(k.params_json, '$.values'), v.sex) > 0;
        UPDATE main.patients SET sex = (
            SELECT CASE WHEN count(*) = 1 THEN min(s.sex) END FROM temp.i_sexes AS s
             WHERE s.target = main.patients.patient_key)
        WHERE patient_key IN (SELECT target FROM temp.i_members);
        INSERT OR REPLACE INTO main.index_checks (object_kind, object_key, code, level, params_json)
        SELECT 'patient', target, 'check.sex_conflict', 'warning', json_object('values', group_concat(sex))
        FROM (SELECT target, sex FROM temp.i_sexes ORDER BY target, sex)
        GROUP BY target HAVING count(*) > 1;

        UPDATE main.studies SET patient_key = (
            SELECT g.target FROM temp.i_rows AS r JOIN temp.i_groups AS g USING (link)
             WHERE r.study_key = main.studies.study_key)
        WHERE study_key IN (SELECT study_key FROM temp.i_rows);

        INSERT OR IGNORE INTO main.patient_links (link, patient_key)
        SELECT link, target FROM temp.i_groups;
        -- A target found through its link keeps the status it has (dicom, file or
        -- confirmed); only the patients this confirmation names become confirmed.
        UPDATE main.patients SET id_status = 'confirmed'
        WHERE id_status = 'unconfirmed' AND patient_key IN (SELECT target FROM temp.i_groups);
        INSERT OR IGNORE INTO main.identifiers (patient_key, patient_id, accession_numbers, id_source)
        SELECT target, label, NULL, id_source FROM temp.i_groups
        WHERE (SELECT keep_identifiers FROM temp.id_param) = 1;

        INSERT INTO main.audit_log (at, actor, action, object, details_json)
        SELECT now, actor, 'ids_confirmed', 'patients',
               json_object('patients', (SELECT count(DISTINCT target) FROM temp.i_groups),
                           'studies', (SELECT count(*) FROM temp.i_rows),
                           'level', level)
        FROM temp.id_param WHERE EXISTS (SELECT 1 FROM temp.i_rows);

        -- Emptied patients go; their keys and pseudonyms are never reused.
        DELETE FROM main.patients
        WHERE patient_key IN (SELECT old_patient FROM temp.i_rows)
          AND patient_key NOT IN (SELECT patient_key FROM main.studies);
        DELETE FROM main.index_checks
        WHERE object_kind = 'patient' AND object_key NOT IN (SELECT patient_key FROM main.patients);

        -- The confirmed studies were held; SelectionSQL.applyAuto and repairPrimary
        -- follow on this scope.
        DELETE FROM temp.sel_scope;
        INSERT INTO temp.sel_scope (study_key) SELECT study_key FROM temp.i_rows;

        DROP TABLE temp.i_rows; DROP TABLE temp.i_groups; DROP TABLE temp.i_members;
        DROP TABLE temp.i_sexes;
        -- The labels are plain identifiers; they do not stay in the connection.
        DELETE FROM temp.id_confirm;
        """

    /// "Assign to Patient…": moves each study in `id_assign` to its patient.
    /// Leaves in `sel_scope` the studies that take the automatic choice, and
    /// writes the audit row `studies_assigned`.
    public static let assignStudy = """
        CREATE TEMP TABLE i_assign AS
        SELECT a.study_key, st.patient_key AS old_patient, a.patient_key AS target,
               op.id_status AS old_status, np.id_status AS new_status, st.selection_mode
        FROM temp.id_assign AS a
        JOIN main.studies AS st USING (study_key)
        JOIN main.patients AS op ON op.patient_key = st.patient_key
        JOIN main.patients AS np ON np.patient_key = a.patient_key
        WHERE st.patient_key <> a.patient_key;

        CREATE TEMP TABLE i_members AS
        SELECT target, target AS patient_key FROM temp.i_assign
        UNION
        SELECT target, old_patient FROM temp.i_assign;
        -- Sex is kept only if every patient joined into a target agrees, as the
        -- merge keeps it only if every study agrees; a patient whose studies already
        -- disagree brings the values its check.sex_conflict lists. The next merge
        -- recomputes both from the studies themselves.
        CREATE TEMP TABLE i_sexes AS
        SELECT m.target, p.sex FROM temp.i_members AS m
        JOIN main.patients AS p ON p.patient_key = m.patient_key WHERE p.sex IS NOT NULL
        UNION
        SELECT m.target, v.sex FROM temp.i_members AS m
        JOIN main.index_checks AS k
          ON k.object_kind = 'patient' AND k.object_key = m.patient_key AND k.code = 'check.sex_conflict'
        JOIN (SELECT 'F' AS sex UNION ALL SELECT 'M' UNION ALL SELECT 'O') AS v
          ON instr(json_extract(k.params_json, '$.values'), v.sex) > 0;
        UPDATE main.patients SET sex = (
            SELECT CASE WHEN count(*) = 1 THEN min(s.sex) END FROM temp.i_sexes AS s
             WHERE s.target = main.patients.patient_key)
        WHERE patient_key IN (SELECT target FROM temp.i_members);
        INSERT OR REPLACE INTO main.index_checks (object_kind, object_key, code, level, params_json)
        SELECT 'patient', target, 'check.sex_conflict', 'warning', json_object('values', group_concat(sex))
        FROM (SELECT target, sex FROM temp.i_sexes ORDER BY target, sex)
        GROUP BY target HAVING count(*) > 1;

        UPDATE main.studies SET patient_key = (
            SELECT a.target FROM temp.i_assign AS a WHERE a.study_key = main.studies.study_key)
        WHERE study_key IN (SELECT study_key FROM temp.i_assign);

        -- The links of a patient left without studies follow its study: the ID they
        -- stand for now names the patient the user chose, so a later study with the
        -- same PatientID joins that patient at the merge instead of making a new one.
        UPDATE main.patient_links SET patient_key = (
            SELECT min(a.target) FROM temp.i_assign AS a WHERE a.old_patient = main.patient_links.patient_key)
        WHERE patient_key IN (SELECT old_patient FROM temp.i_assign)
          AND patient_key NOT IN (SELECT patient_key FROM main.studies);

        INSERT INTO main.audit_log (at, actor, action, object, details_json)
        SELECT now, actor, 'studies_assigned', 'studies',
               json_object('studies', (SELECT count(*) FROM temp.i_assign),
                           'patients_removed', (SELECT count(DISTINCT old_patient) FROM temp.i_assign
                                                 WHERE old_patient NOT IN (SELECT patient_key FROM main.studies)))
        FROM temp.id_param WHERE EXISTS (SELECT 1 FROM temp.i_assign);

        DELETE FROM main.patients
        WHERE patient_key IN (SELECT old_patient FROM temp.i_assign)
          AND patient_key NOT IN (SELECT patient_key FROM main.studies);
        DELETE FROM main.index_checks
        WHERE object_kind = 'patient' AND object_key NOT IN (SELECT patient_key FROM main.patients);

        -- A study under an unconfirmed patient is held, as the merge holds it.
        UPDATE main.series SET is_primary = 0, selected = 0
        WHERE (selected = 1 OR is_primary = 1)
          AND study_key IN (SELECT study_key FROM temp.i_assign WHERE new_status = 'unconfirmed');
        -- An automatic study, and one that was held (nothing could be chosen in it by
        -- hand), takes its automatic choice through SelectionSQL.applyAuto and
        -- repairPrimary on this scope. A study chosen by hand keeps the choice.
        DELETE FROM temp.sel_scope;
        INSERT INTO temp.sel_scope (study_key)
        SELECT study_key FROM temp.i_assign
        WHERE new_status <> 'unconfirmed' AND (selection_mode = 'auto' OR old_status = 'unconfirmed');

        DROP TABLE temp.i_assign; DROP TABLE temp.i_members; DROP TABLE temp.i_sexes;
        """

    /// A typed ID: a new confirmed patient for each link in `id_typed` that no
    /// patient holds, then `id_assign` filled for `assignStudy`, which follows.
    /// Writes the audit row `patients_created`.
    public static let createTypedPatient = """
        -- A typed ID whose link a patient already holds names that patient; each
        -- other link gets a new confirmed patient, numbered like the merge numbers
        -- new patients, from key_counters, so no key or pseudonym is ever reused.
        CREATE TEMP TABLE i_new AS
        SELECT link, min(patient_id) AS patient_id, row_number() OVER (ORDER BY min(study_key)) AS n
        FROM temp.id_typed WHERE link NOT IN (SELECT link FROM main.patient_links) GROUP BY link;

        INSERT INTO main.patients (patient_key, pseudonym, id_status)
        SELECT printf('pt_%06d', (SELECT last FROM main.key_counters WHERE kind = 'patient') + n),
               printf('P%04d', (SELECT last FROM main.key_counters WHERE kind = 'pseudonym') + n),
               'confirmed'
        FROM temp.i_new ORDER BY n;
        INSERT INTO main.patient_links (link, patient_key)
        SELECT link, printf('pt_%06d', (SELECT last FROM main.key_counters WHERE kind = 'patient') + n)
        FROM temp.i_new;
        INSERT INTO main.identifiers (patient_key, patient_id, accession_numbers, id_source)
        SELECT printf('pt_%06d', (SELECT last FROM main.key_counters WHERE kind = 'patient') + n),
               patient_id, NULL, 'typed'
        FROM temp.i_new WHERE (SELECT keep_identifiers FROM temp.id_param) = 1;
        UPDATE main.key_counters SET last = last + (SELECT count(*) FROM temp.i_new)
        WHERE kind IN ('patient', 'pseudonym');

        INSERT INTO main.audit_log (at, actor, action, object, details_json)
        SELECT now, actor, 'patients_created', 'patients',
               json_object('patients', (SELECT count(*) FROM temp.i_new), 'id_source', 'typed')
        FROM temp.id_param WHERE EXISTS (SELECT 1 FROM temp.i_new);

        -- IdentitySQL.assignStudy follows and moves the studies.
        DELETE FROM temp.id_assign;
        INSERT INTO temp.id_assign (study_key, patient_key)
        SELECT t.study_key, l.patient_key FROM temp.id_typed AS t JOIN main.patient_links AS l USING (link);

        DROP TABLE temp.i_new;
        -- The typed IDs are plain identifiers; they do not stay in the connection.
        DELETE FROM temp.id_typed;
        """
}
