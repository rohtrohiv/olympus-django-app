from datetime import datetime, date
import re
import calendar
from collections import defaultdict

from django.core.paginator import Paginator
from django.db import connection
from django.core.cache import cache

from .models import OlympusLeaseTrendAnalysis
from .services import DashboardService


class DashboardPageService:
    """Page-focused service for `dashboard.html`.

    Behavior:
    - For period_mode='year' and period_year='all' uses SQL against
      `web_ai.lease_trend_summary` to fetch latest-per-property rows and KPIs.
    - For other period selections delegates to the existing DashboardService
      to preserve current behavior.
    """

    def __init__(self, params):
        self.params = params or {}

    def _build_where(self):
        where = []
        params = []
        # Support multi-select: getlist for QueryDict or list for dict
        sel_inv = self.params.getlist('investor') if hasattr(self.params, 'getlist') else (
            self.params.get('investor', []) if isinstance(self.params.get('investor', ''), list) else 
            [self.params.get('investor', '')] if self.params.get('investor', '') else []
        )
        sel_mgr = self.params.getlist('regional_manager') if hasattr(self.params, 'getlist') else (
            self.params.get('regional_manager', []) if isinstance(self.params.get('regional_manager', ''), list) else 
            [self.params.get('regional_manager', '')] if self.params.get('regional_manager', '') else []
        )
        sel_comm = self.params.getlist('community') if hasattr(self.params, 'getlist') else (
            self.params.get('community', []) if isinstance(self.params.get('community', ''), list) else 
            [self.params.get('community', '')] if self.params.get('community', '') else []
        )
        # Filter out empty strings
        sel_inv = [i for i in sel_inv if i]
        sel_mgr = [m for m in sel_mgr if m]
        sel_comm = [c for c in sel_comm if c]
        
        # Build WHERE clauses for arrays (use IN)
        if sel_inv:
            placeholders = ','.join(['%s'] * len(sel_inv))
            where.append(f'investor IN ({placeholders})')
            params.extend(sel_inv)
        if sel_mgr:
            placeholders = ','.join(['%s'] * len(sel_mgr))
            where.append(f'regional_area_manager IN ({placeholders})')
            params.extend(sel_mgr)
        if sel_comm:
            placeholders = ','.join(['%s'] * len(sel_comm))
            where.append(f'property_name IN ({placeholders})')
            params.extend(sel_comm)
        # Business rule: exclude BLACKSTONE/LIVCOR for periods after June 2025
        # when no explicit investor filter is provided.
        try:
            # Support multi-month: getlist for period_month, fall back to single value
            period_months = []
            if hasattr(self.params, 'getlist'):
                period_months = self.params.getlist('period_month')
            if not period_months:
                pm_single = self.params.get('period_month') or self.params.get('period')
                if pm_single:
                    period_months = [pm_single]
            period_month = period_months[0] if period_months else None
            period_quarter = self.params.get('period_quarter')
            period_year = self.params.get('period_year')
            def _period_after_jun_2025_p(period_mode, y, m, q):
                import re, calendar
                try:
                    if period_mode == 'month' and m:
                        mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', m)
                        if mm:
                            mon_abbr, yr = mm.groups()
                            mon_num = list(calendar.month_abbr).index(mon_abbr)
                            yr = int(yr)
                            return (yr > 2025) or (yr == 2025 and mon_num > 6)
                    if period_mode == 'quarter' and q:
                        mm = re.search(r'Q(\d)', q, re.I)
                        yy = re.search(r'(\d{4})', q)
                        if mm and yy:
                            qnum = int(mm.group(1))
                            yr = int(yy.group(1))
                            return (yr > 2025) or (yr == 2025 and qnum >= 3)
                    if period_mode == 'year' and y and str(y).isdigit():
                        return int(y) > 2025
                except Exception:
                    return False
                return False

            period_mode = self.params.get('period_mode') or ''
            # Exclude BLACKSTONE/LIVCOR for periods after Jun-2025 unless it was explicitly selected
            sel_up = [s.upper() for s in sel_inv] if sel_inv else []
            if _period_after_jun_2025_p(period_mode, period_year, period_month, period_quarter) and 'BLACKSTONE/LIVCOR' not in sel_up:
                # add exclusion clause
                where = where + ["investor <> %s"]
                params = params + ['BLACKSTONE/LIVCOR']
                where_sql = ('WHERE ' + ' AND '.join(where))
        except Exception:
            pass

        return where_sql, params

    def _fetch_latest_rows(self, where_sql, params):
        # Fetch distinct-on latest-per-property rows
        rows_sql = f"""
        SELECT property_number, property_name, snapshotdate, total_units, occupied_units, percentage_occupacy,
               effective_rent, market_rent, investor, regional_area_manager
        FROM (
          SELECT DISTINCT ON (property_number) property_number, property_name, snapshotdate, total_units, occupied_units, percentage_occupacy,
                 effective_rent, market_rent, investor, regional_area_manager
          FROM web_ai.lease_trend_summary
          {where_sql}
          ORDER BY property_number, snapshotdate DESC
        ) t
        ORDER BY property_name
        ;
        """
        with connection.cursor() as cur:
            cur.execute(rows_sql, params)
            cols = [c[0] for c in cur.description]
            results = [dict(zip(cols, r)) for r in cur.fetchall()]
        return results

    def _fetch_kpi(self, where_sql, params):
        kpi_sql = f"""
        SELECT
          COUNT(*) AS total_properties,
          COALESCE(SUM(total_units),0) AS total_units,
          COALESCE(SUM(occupied_units),0) AS occupied_units,
          COALESCE(AVG(percentage_occupacy),0) AS avg_occupancy
        FROM (
          SELECT DISTINCT ON (property_number) property_number, total_units, occupied_units, percentage_occupacy, snapshotdate
          FROM web_ai.lease_trend_summary
          {where_sql}
          ORDER BY property_number, snapshotdate DESC
        ) t;
        """
        with connection.cursor() as cur:
            cur.execute(kpi_sql, params)
            row = cur.fetchone()
        if row:
            return {
                'total_properties': int(row[0] or 0),
                'total_units': int(row[1] or 0),
                'occupied_units': int(row[2] or 0),
                'avg_occupancy': f"{round(float(row[3] or 0), 2):.2f}%",
                'occupancy': f"{round(float(row[3] or 0), 2):.2f}%",
            }
        return {'total_properties': 0, 'total_units': 0, 'occupied_units': 0, 'avg_occupancy': '0.00%', 'occupancy': '0.00%'}

    def _get_snapshot_range(self, where_sql, params):
        # Determine min and max snapshot_date for the filtered set so we can
        # bound the yearly summary CTE. Returns strings YYYY-MM-DD.
        range_sql = f"SELECT MIN(snapshot_date), MAX(snapshot_date) FROM web_ai.olympus_lease_trend_analysis {where_sql};"
        with connection.cursor() as cur:
            cur.execute(range_sql, params)
            row = cur.fetchone()
        if row and row[0] and row[1]:
            return (row[0].strftime('%Y-%m-%d') if hasattr(row[0], 'strftime') else str(row[0]),
                    row[1].strftime('%Y-%m-%d') if hasattr(row[1], 'strftime') else str(row[1]))
        # fallback to a wide window
        return ('1970-01-01', datetime.now().strftime('%Y-%m-%d'))

    def _fetch_yearly_portfolio_summary(self, where_sql, params, start_date, end_date):
        # Runs the provided yearly aggregation CTE (parameterized start/end dates)
        # Build WHERE clause for the property_yearly_latest CTE. We need to
        # include any provided filter predicate (where_sql) but also inject
        # a business-rule exclusion for BLACKSTONE/LIVCOR rows that occur in
        # 2025 after June when the caller hasn't explicitly filtered by investor.
        base_where = f'"snapshot_date" BETWEEN %s AND %s'
        where_pred = ''
        extra_params = []
        if where_sql:
            # where_sql comes in with a leading 'WHERE ' when provided by _build_where
            where_pred = ' AND ' + where_sql[6:]

        # If caller did not explicitly filter by investor (no investor param in params)
        # then add exclusion of BLACKSTONE/LIVCOR rows for year=2025 and month>6 so that
        # the 2025 yearly aggregates exclude that investor's properties for months after June.
        has_investor_param = any((isinstance(p, str) and p.upper() == 'BLACKSTONE/LIVCOR') for p in params)
        if not has_investor_param:
            where_pred += " AND NOT (investor = %s AND EXTRACT(YEAR FROM snapshot_date) = 2025 AND EXTRACT(MONTH FROM snapshot_date) > 6)"
            extra_params.append('BLACKSTONE/LIVCOR')

        yearly_sql = f"""
        WITH property_yearly_latest AS (
            SELECT DISTINCT ON ("property_number", DATE_TRUNC('year', "snapshot_date"))
                DATE_TRUNC('year', "snapshot_date") AS year,
                "snapshot_date" AS last_snapshot_date,
                "property_number",
                "property_name",
                "investor",
                "regional_area_manager",
                "regional_director",
                "senior_regional",
                "asst_manager",
                "total_units",
                "occupied_units",
                "percentage_occupacy",
                "total_move_ins",
                "total_move_outs",
                "avg_amount_per_sqft",
                "average_rent_move_ins",
                "average_rent_move_outs",
                "effective_rent",
                "market_rent",
                "eff_rent_modified_date",
                "visit",
                "applications",
                "cancelled",
                "denied",
                "vacant_pre_leased",
                "occupied_pre_leased",
                "NTV",
                "make_ready",
                "MTM",
                "current_month_expiring",
                "next_month_expiring",
                "month_after_next_expiring",
                "tbd_90_days",
                "down",
                "vacant_units_without_down_admin"
            FROM web_ai.olympus_lease_trend_analysis
            WHERE {base_where}{where_pred}
            ORDER BY
                "property_number",
                DATE_TRUNC('year', "snapshot_date"),
                "snapshot_date" DESC,
                COALESCE("eff_rent_modified_date", "snapshot_date") DESC
        ),

        yearly_portfolio_summary AS (
            SELECT
                year,
                COUNT(DISTINCT "property_number") AS total_properties,
                MAX(last_snapshot_date) AS last_data_date,
                SUM("total_units") AS total_units_sum,
                SUM("occupied_units") AS total_occupied_units,
                ROUND(
                    CASE
                        WHEN SUM("total_units") > 0 THEN (SUM("occupied_units")::numeric / SUM("total_units")) * 100
                        ELSE NULL
                    END, 2
                ) AS weighted_occupancy_pct,
                SUM("total_move_ins") AS total_move_ins_sum,
                SUM("total_move_outs") AS total_move_outs_sum,
                ROUND(AVG("avg_amount_per_sqft")::numeric, 2) AS avg_rent_per_sqft,
                ROUND(AVG("effective_rent")::numeric, 2) AS avg_effective_rent,
                ROUND(AVG("market_rent")::numeric, 2) AS avg_market_rent,
                SUM("applications") AS total_applications,
                SUM("cancelled") AS total_cancelled,
                SUM("denied") AS total_denied,
                SUM("make_ready") AS total_make_ready,
                SUM("MTM") AS total_mtm,
                SUM("current_month_expiring") AS current_month_expiring_sum,
                SUM("next_month_expiring") AS next_month_expiring_sum
            FROM property_yearly_latest
            GROUP BY year
        )

        SELECT * FROM yearly_portfolio_summary ORDER BY year DESC;
        """
        # parameters: start_date, end_date plus any filter params and any extra exclusion params
        exec_params = [start_date, end_date] + params + extra_params
        with connection.cursor() as cur:
            cur.execute(yearly_sql, exec_params)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows

    def _fetch_unique_properties_raw(self, where_sql, params):
        # Use the DISTINCT ON query provided by the user to get one latest row per property
        # If where_sql is present it should already include the leading WHERE
        # Join with KPIs table to get delinquency data for the selected period
        
        # Parse the selected period from params to get the appropriate month
        import re
        import calendar
        period_month = None
        period_months = []
        
        if hasattr(self.params, 'getlist'):
            period_months = self.params.getlist('period_month')
        if not period_months:
            pm_single = self.params.get('period_month') or self.params.get('period')
            if pm_single:
                period_months = [pm_single]
        
        # Use the first selected period month if available
        if period_months and period_months[0]:
            period_str = period_months[0]
            # Parse format like "Oct-2025"
            mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', period_str)
            if mm:
                mon_abbr, yr = mm.groups()
                try:
                    mon_num = list(calendar.month_abbr).index(mon_abbr)
                    yr = int(yr)
                    # Get the last day of the selected month for matching
                    last_day = calendar.monthrange(yr, mon_num)[1]
                    period_month = f"{yr}-{mon_num:02d}-{last_day:02d}"
                except (ValueError, IndexError):
                    pass
        
        # Build the KPI join condition based on whether we have a specific period
        if period_month:
            # Match exact month for the selected period
            kpi_join = f'''
            LEFT JOIN LATERAL (
                SELECT "delinquency"
                FROM web_ai.olympus_lease_kpis_trend_monthly
                WHERE "property_number" = olt."property_number"
                    AND "enddateofmonth" = '{period_month}'::date
                LIMIT 1
            ) kpi_latest ON true
            '''
        else:
            # No specific period selected, get most recent KPI data
            kpi_join = '''
            LEFT JOIN LATERAL (
                SELECT "delinquency"
                FROM web_ai.olympus_lease_kpis_trend_monthly
                WHERE "property_number" = olt."property_number"
                    AND "enddateofmonth" <= olt."snapshot_date"
                ORDER BY "enddateofmonth" DESC
                LIMIT 1
            ) kpi_latest ON true
            '''
        
        rows_sql = f'''
        SELECT DISTINCT ON (olt."property_number")
            olt."property_number",
            olt."property_name",
            olt."investor",
            olt."regional_area_manager",
            olt."regional_director",
            olt."senior_regional",
            olt."asst_manager",
            olt."snapshot_date" AS latest_snapshot_date,
            olt."total_units",
            olt."occupied_units",
            ROUND(
                CASE 
                    WHEN olt."total_units" > 0 THEN (olt."occupied_units"::numeric / olt."total_units") * 100
                    ELSE NULL
                END, 2
            ) AS occupancy_pct,
            olt."effective_rent",
            olt."market_rent",
            kpi_latest."delinquency"
        FROM web_ai.olympus_lease_trend_analysis olt
        {kpi_join}
        {where_sql}
        ORDER BY 
            olt."property_number", 
            olt."snapshot_date" DESC,
            COALESCE(olt."eff_rent_modified_date", olt."snapshot_date") DESC;
        '''
        with connection.cursor() as cur:
            cur.execute(rows_sql, params)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows

    def _build_charts_from_rows(self, rows, period_mode, period_year, period_month):
        # Build chart series similarly to the original implementation but
        # grouping by year for the "all" view.
        buckets = {}
        group_by_year = (period_mode == 'year' and str(period_year).lower() == 'all')
        for r in rows:
            try:
                sd = r.get('snapshotdate')
                if isinstance(sd, str):
                    try:
                        sd = datetime.strptime(sd, '%Y-%m-%d').date()
                    except Exception:
                        sd = None
                if not sd:
                    continue
                key = sd.year if group_by_year else date(sd.year, sd.month, 1)
                prop = r.get('property_number') or r.get('property_name')
                prop_key = str(prop)
                bucket = buckets.setdefault(key, {})
                cur = bucket.get(prop_key)
                if cur is None or sd > cur[0]:
                    bucket[prop_key] = (sd, r)
            except Exception:
                continue

        labels = []
        eff_series = []
        mkt_series = []
        occ_series = []
        rev_series = []
        for key in sorted(buckets.keys()):
            bucket = buckets[key]
            if isinstance(key, int):
                labels.append(str(key))
            else:
                labels.append(key.strftime('%b %Y'))
            eff_vals = []
            mkt_vals = []
            occ_vals = []
            rev_sum = 0.0
            for prop_key, (sd, row) in bucket.items():
                eff = row.get('effective_rent')
                mkt = row.get('market_rent')
                occ = row.get('percentage_occupacy')
                if eff not in (None, ''):
                    try:
                        eff_vals.append(float(eff))
                    except Exception:
                        pass
                if mkt not in (None, ''):
                    try:
                        mkt_vals.append(float(mkt))
                    except Exception:
                        pass
                try:
                    if occ not in (None, '', 'None'):
                        occ_vals.append(float(occ))
                except Exception:
                    pass
                try:
                    occ_units = float(row.get('occupied_units') or 0)
                    if eff not in (None, ''):
                        rev_sum += (float(eff) * occ_units)
                except Exception:
                    pass

            eff_series.append(round(sum(eff_vals) / len(eff_vals), 2) if eff_vals else 0)
            mkt_series.append(round(sum(mkt_vals) / len(mkt_vals), 2) if mkt_vals else 0)
            occ_series.append(round(sum(occ_vals) / len(occ_vals), 2) if occ_vals else 0)
            rev_series.append(round(rev_sum / 1000.0, 2))

        chart_rent = {
            'labels': labels,
            'datasets': [
                {'label': 'Effective', 'data': eff_series, 'borderColor': '#0E555A', 'backgroundColor': 'transparent', 'borderWidth': 2, 'tension': 0.3},
                {'label': 'Market', 'data': mkt_series, 'borderColor': '#59E6F6', 'backgroundColor': 'transparent', 'borderWidth': 2, 'tension': 0.3},
            ]
        }

        chart_occrev = {
            'labels': labels,
            'datasets': [
                {'type': 'line', 'label': 'Occupancy %', 'yAxisID': 'yL', 'data': occ_series, 'borderColor': '#59E6F6', 'backgroundColor': 'transparent', 'borderWidth': 2, 'tension': 0.3},
                {'label': 'Total Revenue (k$)', 'yAxisID': 'yR', 'data': rev_series, 'backgroundColor': '#C69A58'},
            ]
        }

        properties_count = [len(buckets[k]) for k in sorted(buckets.keys())]
        units_data = []
        occ_data = []
        for key in sorted(buckets.keys()):
            bucket = buckets[key]
            total_units_sum = 0
            occ_vals = []
            for prop_key, (sd, row) in bucket.items():
                try:
                    total_units_sum += int(row.get('total_units') or 0)
                except Exception:
                    try:
                        total_units_sum += int(float(row.get('total_units') or 0))
                    except Exception:
                        pass
                try:
                    occ_val = row.get('percentage_occupacy')
                    if occ_val not in (None, '', 'None'):
                        occ_vals.append(float(occ_val))
                except Exception:
                    pass
            units_data.append(total_units_sum)
            occ_data.append(round(sum(occ_vals) / len(occ_vals), 2) if occ_vals else 0)

        chart_properties = {
            'years': labels,
            'propertiesData': properties_count,
            'unitsData': units_data,
            'occupancyData': occ_data,
        }

        return chart_rent, chart_occrev, chart_properties

    def get_context(self):
        # Build periods using the original service to keep consistency
        svc = DashboardService(self.params)
        sorted_periods = svc.build_periods()

        period_mode = self.params.get('period_mode') or ''
        period_year = self.params.get('period_year') or ''
        period_quarter = self.params.get('period_quarter') or ''
        period_month = self.params.get('period_month') or ''
        selected_period = self.params.get('period')
        if period_mode == 'year' and str(period_year).lower() == 'all':
            selected_period = 'All time'

        use_sql_summary = (period_mode == 'year' and str(period_year).lower() == 'all')
        if not use_sql_summary:
            # Delegate to the existing service for non-all behavior
            return svc.get_context()

        where_sql, params = self._build_where()
        # Fetch property-level latest rows (for property table) and yearly aggregates
        rows = self._fetch_unique_properties_raw(where_sql, params)

        # Normalize keys so downstream chart builders that expect
        # 'snapshotdate' and 'percentage_occupacy' still work if needed.
        for r in rows:
            # latest_snapshot_date -> snapshotdate
            if 'latest_snapshot_date' in r and 'snapshotdate' not in r:
                r['snapshotdate'] = r.get('latest_snapshot_date')
            # occupancy_pct -> percentage_occupacy
            if 'occupancy_pct' in r and 'percentage_occupacy' not in r:
                r['percentage_occupacy'] = r.get('occupancy_pct')

        # Determine snapshot date range and fetch yearly portfolio summary
        start_date, end_date = self._get_snapshot_range(where_sql, params)
        yearly_rows = self._fetch_yearly_portfolio_summary(where_sql, params, start_date, end_date)

        # Derive KPI: prefer most recent year summary when available
        if yearly_rows:
            # yearly_rows ordered DESC by year
            latest_year = yearly_rows[0]
            kpi = {
                'total_properties': int(latest_year.get('total_properties') or 0),
                'total_units': int(latest_year.get('total_units_sum') or 0),
                'occupied_units': int(latest_year.get('total_occupied_units') or 0),
                'avg_occupancy': f"{(latest_year.get('weighted_occupancy_pct') or 0):.2f}%",
                'occupancy': f"{(latest_year.get('weighted_occupancy_pct') or 0):.2f}%",
            }
        else:
            # Fallback to latest-per-property KPI
            kpi = self._fetch_kpi(where_sql, params)

        # Build paginated properties_page and properties_list
        # Build paginated properties_page and properties_list from the full SQL rows
        # rows already contains the full DISTINCT ON result with fields like
        # latest_snapshot_date, occupancy_pct, effective_rent, market_rent, etc.
        prop_values = rows
        paginator = Paginator(prop_values, 10)
        page = int(self.params.get('page', 1)) if str(self.params.get('page','1')).isdigit() else 1
        properties_page = paginator.get_page(page)

        # Build filters lists (reuse original pattern)
        distinct_rows = list(OlympusLeaseTrendAnalysis.objects.values('investor','regional_area_manager','property_name').distinct())
        prop_set = set(); inv_set = set(); mgr_set = set()
        investor_properties = {}; investor_managers = {}; property_managers = {}
        for row in distinct_rows:
            inv = (row.get('investor') or '').strip()
            rm = (row.get('regional_area_manager') or '').strip()
            prop = (row.get('property_name') or '').strip()
            if prop: prop_set.add(prop)
            if inv: inv_set.add(inv)
            if rm: mgr_set.add(rm)
            if inv: investor_properties.setdefault(inv, set()).add(prop); investor_managers.setdefault(inv, set()).add(rm)
            if prop: property_managers.setdefault(prop, set()).add(rm)
        properties = sorted([p for p in prop_set if p])
        investors = sorted([i for i in inv_set if i])
        managers = sorted([m for m in mgr_set if m])
        investor_properties = {k: sorted([p for p in v if p]) for k, v in investor_properties.items()}
        investor_managers = {k: sorted([m for m in v]) for k, v in investor_managers.items()}
        property_managers = {k: sorted([m for m in v]) for k, v in property_managers.items()}

        # Build charts from yearly summary if available, otherwise from rows
        if yearly_rows:
            # prepare labels and series from yearly_rows
            labels = []
            eff_series = []
            mkt_series = []
            occ_series = []
            rev_series = []
            props_count = []
            units_data = []
            for yr in sorted(yearly_rows, key=lambda x: x.get('year'), reverse=False):
                y = yr.get('year')
                # year may be date-like or string
                if hasattr(y, 'year'):
                    labels.append(str(y.year))
                else:
                    labels.append(str(y)[:4])
                eff = yr.get('avg_effective_rent') or 0
                mkt = yr.get('avg_market_rent') or 0
                occ = float(yr.get('weighted_occupancy_pct') or 0)
                units = int(yr.get('total_units_sum') or 0)
                props = int(yr.get('total_properties') or 0)
                rev_est = float(eff or 0) * float(yr.get('total_occupied_units') or 0)
                eff_series.append(float(eff))
                mkt_series.append(float(mkt))
                occ_series.append(round(occ, 2))
                rev_series.append(round(rev_est / 1000.0, 2))
                props_count.append(props)
                units_data.append(units)

            chart_rent = {
                'labels': labels,
                'datasets': [
                    {'label': 'Effective', 'data': eff_series, 'borderColor': '#0E555A', 'backgroundColor': 'transparent', 'borderWidth': 2, 'tension': 0.3},
                    {'label': 'Market', 'data': mkt_series, 'borderColor': '#59E6F6', 'backgroundColor': 'transparent', 'borderWidth': 2, 'tension': 0.3},
                ]
            }

            chart_occrev = {
                'labels': labels,
                'datasets': [
                    {'type': 'line', 'label': 'Occupancy %', 'yAxisID': 'yL', 'data': occ_series, 'borderColor': '#59E6F6', 'backgroundColor': 'transparent', 'borderWidth': 2, 'tension': 0.3},
                    {'label': 'Total Revenue (k$)', 'yAxisID': 'yR', 'data': rev_series, 'backgroundColor': '#C69A58'},
                ]
            }

            chart_properties = {
                'years': labels,
                'propertiesData': props_count,
                'unitsData': units_data,
                'occupancyData': occ_series,
            }
        else:
            chart_rent, chart_occrev, chart_properties = self._build_charts_from_rows(rows, period_mode, period_year, period_month)

        return {
            'periods': sorted_periods,
            'period_mode': period_mode,
            'period_year': period_year,
            'period_quarter': period_quarter,
            'period_month': period_month,
            'selected_period': selected_period,
            'kpi': kpi,
            'properties_page': properties_page,
            'properties_list': prop_values,
            'investors': investors,
            'managers': managers,
            'investor_properties': investor_properties,
            'investor_managers': investor_managers,
            'property_managers': property_managers,
            'chart_rent': chart_rent,
            'chart_occrev': chart_occrev,
            'chart_properties': chart_properties,
        }
