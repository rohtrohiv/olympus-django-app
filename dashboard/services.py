from collections import defaultdict
from datetime import datetime, date, timedelta
import re
import threading
import calendar

from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import IntegerField, Max, Avg, Q, Sum
from django.db.models.functions import ExtractYear, ExtractMonth
from django.db import connection

from .models import OlympusLeaseTrendAnalysis, LeaseTrendSummary


class DashboardService:
    """Service object that encapsulates dashboard-related queries and computations.

    Instantiate with a params-like dict (request.GET) and then call `get_context()`
    to receive the final context dict for rendering.
    """

    def __init__(self, params):
        self.params = params or {}

    @staticmethod
    def infer_period_from_legacy(legacy_period):
        if not legacy_period:
            return None, None
        if re.match(r'^\d{4}$', legacy_period):
            return 'year', legacy_period
        elif re.match(r'^[A-Za-z]{3}-\d{4}$', legacy_period) or re.match(r'^[A-Za-z]{3} \d{4}$', legacy_period):
            return 'month', legacy_period
        elif re.match(r'^Q\d[-_]?\d{4}$', legacy_period, re.I):
            return 'quarter', legacy_period
        return None, None

    def build_periods(self):
        """Return structured periods dict used by the UI: {year: {months: [...], quarters: {...}}}

        This encapsulates the DB query that extracts distinct year/month pairs.
        """
        periods = {}
        year_month_qs = (
            OlympusLeaseTrendAnalysis.objects
            .filter(snapshotdate__isnull=False)
            .annotate(
                year=ExtractYear('snapshotdate', output_field=IntegerField()),
                month=ExtractMonth('snapshotdate', output_field=IntegerField()),
            )
            .values('year', 'month')
            .distinct()
        )
        for ym in year_month_qs:
            y = ym.get('year')
            m = ym.get('month')
            if y is None or m is None:
                continue
            year = str(y)
            month_name = datetime.strptime(f"{m:02d}", "%m").strftime("%b")
            periods.setdefault(year, {"months": set(), "quarters": {}})
            periods[year]["months"].add(month_name)
            q = (m - 1) // 3 + 1
            periods[year]["quarters"].setdefault(f"Q{q}", set()).add(month_name)

        # Sort years descending, months/quarters ascending
        sorted_periods = {}
        for year in sorted(periods.keys(), reverse=True):
            sorted_periods[year] = {
                "months": sorted(periods[year]["months"], key=lambda m: datetime.strptime(m, "%b")),
                "quarters": {q: sorted(periods[year]["quarters"][q], key=lambda m: datetime.strptime(m, "%b")) for q in sorted(periods[year]["quarters"].keys())}
            }
        return sorted_periods

    def _to_date_generic(self, dv):
        """Normalize various snapshotdate formats to a date object or None."""
        if dv is None:
            return None
        if isinstance(dv, date):
            return dv
        try:
            return datetime.strptime(str(dv), '%Y-%m-%d').date()
        except Exception:
            pass
        m = str(dv)
        if '-' in m:
            parts = m.split('-')
            if len(parts) == 2 and len(parts[1]) == 4:
                mn = parts[0]
                try:
                    month_num = list(calendar.month_abbr).index(mn)
                    return date(int(parts[1]), month_num, 1)
                except Exception:
                    pass
        try:
            return datetime.fromisoformat(str(dv)).date()
        except Exception:
            return None

    def _pick_last_day_from_qs(self, filtered_qs):
        """Return a queryset limited to rows matching the latest snapshotdate in filtered_qs."""
        if not filtered_qs.exists():
            return filtered_qs.none()
        max_date = filtered_qs.aggregate(max_date=Max('snapshotdate'))['max_date']
        return filtered_qs.filter(snapshotdate=max_date) if max_date else filtered_qs.none()

    def warm_full_dataset_cache(self, filter_kwargs=None, cache_key='lease_trend_full_data', timeout=60*60):
        """Load the full dataset (or filtered) into cache (background safe)."""
        def _job():
            try:
                qs_full = OlympusLeaseTrendAnalysis.objects.all()
                if filter_kwargs:
                    qs_full = qs_full.filter(**filter_kwargs)
                fields = [
                    'property_number',
                    'property_name',
                    'snapshotdate',
                    'total_units',
                    'occupied_units',
                    'percentage_occupacy',
                    'total_move_ins',
                    'total_move_outs',
                    'applications',
                    'avg_amount_per_sqft',
                    'effective_rent',
                    'market_rent',
                    'investor',
                    'regional_area_manager',
                    'regional_director',
                    'vacant_units_without_down_admin',
                ]
                data = list(qs_full.values(*fields))
                cache.set(cache_key, data, timeout=timeout)
            except Exception:
                import traceback; traceback.print_exc()
        threading.Thread(target=_job, daemon=True).start()


    def get_context(self):
        # periods
        sorted_periods = self.build_periods()

        # read params
        period_mode = self.params.get('period_mode') or ''
        period_year = self.params.get('period_year') or ''
        period_quarter = self.params.get('period_quarter') or ''
        period_month = self.params.get('period_month') or ''
        legacy_period = self.params.get('period')
        if not period_mode and legacy_period:
            mode, val = self.infer_period_from_legacy(legacy_period)
            if mode == 'year':
                period_mode, period_year = 'year', val
            elif mode == 'month':
                period_mode, period_month = 'month', val
            elif mode == 'quarter':
                period_mode, period_quarter = 'quarter', val

        if not period_mode:
            period_mode = 'month'

        selected_period = self.params.get('period')
        # If the user explicitly requested 'all' years, present a friendly label
        if period_mode == 'year' and str(period_year).lower() == 'all':
            selected_period = 'All time'
        if not (period_year or period_quarter or period_month or selected_period) and sorted_periods:
            latest_year = next(iter(sorted(sorted_periods.keys(), reverse=True)))
            latest_quarter = next(iter(sorted(sorted_periods[latest_year]['quarters'].keys(), reverse=True)))
            latest_month = sorted(sorted_periods[latest_year]['quarters'][latest_quarter], key=lambda m: datetime.strptime(m, "%b"))[-1]
            selected_period = f"{latest_month}-{latest_year}"
            period_mode = 'month'
            period_month = selected_period

        # Build base queryset and apply simple filters from params
        qs = OlympusLeaseTrendAnalysis.objects.all()
        selected_investor = self.params.get('investor', '')
        selected_regional_manager = self.params.get('regional_manager', '')
        selected_community = self.params.get('community', '')
        if selected_investor:
            qs = qs.filter(investor=selected_investor)
        if selected_regional_manager:
            qs = qs.filter(regional_area_manager=selected_regional_manager)
        if selected_community:
            qs = qs.filter(property_name=selected_community)

        # Determine the initial latest-month selection: prefer explicit period params, otherwise latest available
        if not (period_year or period_quarter or period_month or selected_period) and sorted_periods:
            latest_year = next(iter(sorted(sorted_periods.keys(), reverse=True)))
            latest_quarter = next(iter(sorted(sorted_periods[latest_year]['quarters'].keys(), reverse=True)))
            latest_month = sorted(sorted_periods[latest_year]['quarters'][latest_quarter], key=lambda m: datetime.strptime(m, "%b"))[-1]
            selected_period = f"{latest_month}-{latest_year}"
            period_mode = 'month'
            period_month = selected_period

        # Build a filtered queryset for the selected period (only latest month on initial load)
        period_qs = qs
        if period_mode == 'month' and period_month:
            mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', period_month)
            if mm:
                month_name, year = mm.groups()
                month_num = datetime.strptime(month_name, "%b").strftime("%m")
                period_qs = qs.filter(Q(snapshotdate__startswith=f"{year}-{month_num}") | Q(snapshotdate__icontains=f"{month_name}-{year}"))
        elif period_mode == 'quarter' and period_quarter:
            parts = re.split('[-_]', period_quarter)
            qpart = parts[0].upper()
            year = parts[-1]
            qnum = int(re.sub('[^0-9]','', qpart))
            start_month = (qnum-1)*3 + 1
            months_nums = [ f"{m:02d}" for m in range(start_month, start_month+3) ]
            q_filter = Q()
            for mnum in months_nums:
                q_filter |= Q(snapshotdate__startswith=f"{year}-{mnum}")
            period_qs = qs.filter(q_filter)
        elif period_mode == 'year' and period_year:
            # Support 'all' sentinel to indicate all available years
            if str(period_year).lower() == 'all':
                period_qs = qs
            else:
                year = period_year
                period_qs = qs.filter(Q(snapshotdate__startswith=f"{year}-") | Q(snapshotdate__endswith=f"-{year}"))

        # Narrow to last-day snapshot within selected period for KPI cards (dedupe per property)
        # If using the summary MV, compute KPI directly from it to avoid scanning large raw table
        use_summary = (period_mode == 'year' and str(period_year).lower() == 'all')
        if use_summary:
            # Build a summary queryset filtered by any active filters
            summary_qs = LeaseTrendSummary.objects.all()
            if selected_investor:
                summary_qs = summary_qs.filter(investor=selected_investor)
            if selected_regional_manager:
                summary_qs = summary_qs.filter(regional_area_manager=selected_regional_manager)
            if selected_community:
                summary_qs = summary_qs.filter(property_name=selected_community)
            # Use the summary queryset as the source for KPI/table/chart generation
            last_day_qs = summary_qs
        else:
            last_day_qs = self._pick_last_day_from_qs(period_qs)
            if not last_day_qs.exists():
                # fallback: limit to latest 30 days
                today = datetime.now().date()
                thirty_days_ago = today - timedelta(days=30)
                last_day_qs = qs.filter(snapshotdate__gte=thirty_days_ago)

        # Compute KPI summary from last_day_qs (dedup per property using latest snapshot)
        total_properties = 0
        total_units = 0
        occupied_units = 0
        avg_occupancy = 0

        if use_summary:
            # Use raw SQL: pick latest snapshot per property from the summary view (DISTINCT ON)
            where_clauses = []
            params = []
            if selected_investor:
                where_clauses.append("investor = %s")
                params.append(selected_investor)
            if selected_regional_manager:
                where_clauses.append("regional_area_manager = %s")
                params.append(selected_regional_manager)
            if selected_community:
                where_clauses.append("property_name = %s")
                params.append(selected_community)
            where_sql = ('WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

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
                    total_properties = int(row[0] or 0)
                    total_units = int(row[1] or 0)
                    occupied_units = int(row[2] or 0)
                    avg_occupancy = round(float(row[3] or 0), 2)
        else:
            if last_day_qs.exists():
                unique_props = last_day_qs.values('property_number', 'property_name', 'total_units').distinct()
                total_properties = unique_props.count()
                total_units = sum([row['total_units'] or 0 for row in unique_props])
                occ = last_day_qs.aggregate(occ=Avg('percentage_occupacy'))['occ']
                avg_occupancy = round(occ or 0, 2)
                occupied_units = last_day_qs.aggregate(sum_occ=Sum('occupied_units'))['sum_occ'] or 0

        kpi = {
            'total_properties': total_properties,
            'total_units': total_units,
            'occupied_units': occupied_units,
            'avg_occupancy': f"{avg_occupancy:.2f}%",
            # legacy template key expected by _kpi_cards.html
            'occupancy': f"{avg_occupancy:.2f}%",
        }

        # Capture the snapshot date used for KPI (latest snapshot within the selected period)
        kpi_snapshot_date = None
        try:
            if last_day_qs.exists():
                kpi_snapshot_date = last_day_qs.aggregate(max_date=Max('snapshotdate'))['max_date']
        except Exception:
            kpi_snapshot_date = None

        # Table: prepare a small paginated property list from last_day_qs (10 per page)
        prop_values = last_day_qs.values(
            'property_number', 'property_name', 'total_units', 'investor', 'regional_area_manager',
            'regional_director', 'asst_manager', 'occupied_units'
        ).distinct()
        # Use in-memory paginator so view can render `properties` as page object
        paginator = Paginator(list(prop_values), 10)
        page = int(self.params.get('page', 1)) if str(self.params.get('page','1')).isdigit() else 1
        properties_page = paginator.get_page(page)

        # Build a full (non-paginated) properties list used by modals (contains dicts
        # with property_name and total_units). Using prop_values ensures this list
        # reflects the same latest-per-property rows used for the table/pagination.
        try:
            prop_list = list(prop_values)
            # Include additional fields so modals and client-side JSON have
            # investor / manager / director / asst_manager / occupied_units
            properties_full = sorted([
                {
                    'property_number': r.get('property_number'),
                    'property_name': r.get('property_name'),
                    'total_units': int(r.get('total_units') or 0),
                    'occupied_units': int(r.get('occupied_units') or 0),
                    'investor': r.get('investor') or '',
                    'regional_area_manager': r.get('regional_area_manager') or '',
                    'regional_director': r.get('regional_director') or '',
                    'asst_manager': r.get('asst_manager') or ''
                } for r in prop_list
            ], key=lambda x: (x['property_name'] or '').lower())
        except Exception:
            properties_full = []

        # --- Time series charts: chartRent and chartOccRev ---
        # Prepare rows for the selected period (period_qs already filtered to the period)
        # If user requested all years in year-mode, read from the pre-aggregated
        # materialized view via LeaseTrendSummary to avoid scanning the raw table.
        rows = []
        use_summary = (period_mode == 'year' and str(period_year).lower() == 'all')
        if use_summary:
            # Query the summary view: one row per property-year (deduped in SQL)
            qs_summary = LeaseTrendSummary.objects.all()
            if selected_investor:
                qs_summary = qs_summary.filter(investor=selected_investor)
            if selected_regional_manager:
                qs_summary = qs_summary.filter(regional_area_manager=selected_regional_manager)
            if selected_community:
                qs_summary = qs_summary.filter(property_name=selected_community)

            # Pull into a small list of dicts compatible with original downstream code
            rows = list(qs_summary.values(
                'property_number', 'property_name', 'snapshotdate',
                'effective_rent', 'market_rent', 'occupied_units', 'percentage_occupacy', 'total_units'
            ))
            # Normalize keys to match previous names expected by the chart building
            normalized_rows = []
            for r in rows:
                normalized_rows.append({
                    'property_number': r.get('property_number'),
                    'property_name': r.get('property_name'),
                    'snapshotdate': r.get('snapshotdate'),
                    'effective_rent': r.get('effective_rent'),
                    'market_rent': r.get('market_rent'),
                    'occupied_units': r.get('occupied_units'),
                    'percentage_occupacy': r.get('percentage_occupacy'),
                    'total_units': r.get('total_units')
                })
            rows = normalized_rows
        else:
            rows = list(period_qs.values(
                'property_number', 'property_name', 'snapshotdate',
                'effective_rent', 'market_rent', 'occupied_units', 'percentage_occupacy', 'total_units'
            ))

        # Helper: build bucket key depending on mode
        buckets = {}  # bucket_key -> { prop_key -> (snap_date, row) }

        if period_mode == 'month':
            # Bucket by week within the selected month: Week 1 = days 1-7, Week 2 = 8-14, etc.
            # Parse selected month/year
            mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', period_month or '')
            if mm:
                month_name_sel, year_sel = mm.groups()
                try:
                    month_num_sel = datetime.strptime(month_name_sel, "%b").month
                    year_num_sel = int(year_sel)
                except Exception:
                    month_num_sel = None
                    year_num_sel = None
            else:
                month_num_sel = None
                year_num_sel = None

            # compute number of weeks in month (7-day buckets starting at day 1)
            if month_num_sel and year_num_sel:
                _, days_in_month = calendar.monthrange(year_num_sel, month_num_sel)
                num_weeks = (days_in_month + 6) // 7
            else:
                num_weeks = 5

            # initialize empty buckets for week indices 0..num_weeks-1
            for wi in range(num_weeks):
                buckets.setdefault(wi, {})

            for r in rows:
                sd = self._to_date_generic(r.get('snapshotdate'))
                if not sd:
                    continue
                # skip rows outside selected month/year
                if month_num_sel and year_num_sel:
                    if sd.year != year_num_sel or sd.month != month_num_sel:
                        continue
                day = sd.day
                bucket_idx = (day - 1) // 7
                if bucket_idx >= num_weeks:
                    bucket_idx = num_weeks - 1
                prop = r.get('property_number') or r.get('property_name')
                prop_key = str(prop)
                bucket = buckets.setdefault(bucket_idx, {})
                cur = bucket.get(prop_key)
                if cur is None or sd > cur[0]:
                    bucket[prop_key] = (sd, r)

        else:
            # quarter or year -> group by month normally, but if user requested ALL years
            # and we're in year mode, group by year (one bucket per year)
            group_by_year = (period_mode == 'year' and str(period_year).lower() == 'all')
            for r in rows:
                sd = self._to_date_generic(r.get('snapshotdate'))
                if not sd:
                    continue
                if group_by_year:
                    key = sd.year  # integer year as bucket key
                else:
                    key = date(sd.year, sd.month, 1)
                prop = r.get('property_number') or r.get('property_name')
                prop_key = str(prop)
                bucket = buckets.setdefault(key, {})
                cur = bucket.get(prop_key)
                if cur is None or sd > cur[0]:
                    bucket[prop_key] = (sd, r)

        # Aggregate per bucket into series
        labels = []
        eff_series = []
        mkt_series = []
        occ_series = []
        rev_series = []

        for key in sorted(buckets.keys()):
            bucket = buckets[key]
            if period_mode == 'month':
                # Prefer month range labels like 'Aug 1–7'
                try:
                    mname = month_name_sel
                    start_day = key * 7 + 1
                    end_day = min((key + 1) * 7, days_in_month)
                    labels.append(f"{mname} {start_day}–{end_day}")
                except Exception:
                    labels.append(f"Week {key+1}")
            else:
                # If we've used integer year keys, render as '2024'
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
                occ_n = None
                try:
                    if occ not in (None, '', 'None'):
                        occ_n = float(str(occ))
                except Exception:
                    occ_n = None
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
                if occ_n is not None:
                    occ_vals.append(occ_n)
                # revenue estimate: effective_rent * occupied_units
                try:
                    occ_units = float(row.get('occupied_units') or 0)
                    if eff not in (None, ''):
                        rev_sum += (float(eff) * occ_units)
                except Exception:
                    pass

            eff_series.append(round(sum(eff_vals) / len(eff_vals), 2) if eff_vals else 0)
            mkt_series.append(round(sum(mkt_vals) / len(mkt_vals), 2) if mkt_vals else 0)
            occ_series.append(round(sum(occ_vals) / len(occ_vals), 2) if occ_vals else 0)
            rev_series.append(round(rev_sum / 1000.0, 2))  # k$

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

        # Chart for modal total properties: count unique properties per bucket
        properties_count = [len(buckets[k]) for k in sorted(buckets.keys())]

        # Compute total units and average occupancy per bucket
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


        # Filters: build lists/maps used by UI
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
        investor_managers = {k: sorted([m for m in v if m]) for k, v in investor_managers.items()}
        property_managers = {k: sorted([m for m in v]) for k, v in property_managers.items()}

        # Start background cache warm if empty
        if cache.get('lease_trend_full_data') is None:
            self.warm_full_dataset_cache()

        # Compose final context returned to view/template
        return {
            'periods': sorted_periods,
            'period_mode': period_mode,
            'period_year': period_year,
            'period_quarter': period_quarter,
            'period_month': period_month,
            'selected_period': selected_period,
            'kpi': kpi,
            'properties_page': properties_page,
            'properties_list': properties_full,
            'investors': investors,
            'managers': managers,
            'investor_properties': investor_properties,
            'investor_managers': investor_managers,
            'property_managers': property_managers,
            'chart_rent': chart_rent,
            'chart_occrev': chart_occrev,
            'chart_properties': chart_properties,
            'kpi_snapshot_date': kpi_snapshot_date,
        }



