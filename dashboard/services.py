from collections import defaultdict
from datetime import datetime, date, timedelta
import re
import threading
import calendar

from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import IntegerField, Max, Avg, Q, Sum, F, OuterRef, Subquery
from django.db.models.functions import ExtractYear, ExtractMonth
from django.db import connection

from .models import OlympusLeaseTrendAnalysis, LeaseTrendSummary, OlympusLeaseKpisTrendMonthly, OlympusLeaseMoveoutReasonsTrendMonthly


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

    def _get_previous_month_period(self, sorted_periods):
        """Calculate the previous month's period string from available data.
        
        Returns the previous month relative to the current date if it exists in the data,
        otherwise returns the most recent month available in the data.
        This ensures we show complete data rather than incomplete current month data.
        
        Returns: tuple of (period_string, period_mode) e.g. ("Oct-2025", "month")
        """
        if not sorted_periods:
            return None, None
            
        # Get current date
        today = datetime.now()
        current_year = today.year
        current_month = today.month
        
        # Calculate previous month
        if current_month == 1:
            prev_month = 12
            prev_year = current_year - 1
        else:
            prev_month = current_month - 1
            prev_year = current_year
        
        prev_month_name = datetime.strptime(f"{prev_month:02d}", "%m").strftime("%b")
        prev_year_str = str(prev_year)
        
        # Check if previous month exists in our data
        if prev_year_str in sorted_periods:
            if prev_month_name in sorted_periods[prev_year_str]["months"]:
                return f"{prev_month_name}-{prev_year_str}", "month"
        
        # If previous month doesn't exist, fall back to the latest available month
        latest_year = next(iter(sorted(sorted_periods.keys(), reverse=True)))
        latest_quarter = next(iter(sorted(sorted_periods[latest_year]['quarters'].keys(), reverse=True)))
        latest_month = sorted(sorted_periods[latest_year]['quarters'][latest_quarter], key=lambda m: datetime.strptime(m, "%b"))[-1]
        
        return f"{latest_month}-{latest_year}", "month"

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
    
    def _pick_latest_per_property_from_qs(self, filtered_qs):
        """
        Return a queryset with the latest snapshot per property from filtered_qs.
        This is useful when multiple periods are selected - we want to show each property's
        most recent data across all selected periods.
        """
        if not filtered_qs.exists():
            return filtered_qs.none()
        
        # Get property numbers and their latest dates
        from django.db.models import Max
        latest_by_property = filtered_qs.values('property_number').annotate(
            latest_date=Max('snapshotdate')
        )
        
        # Build a list of (property_number, latest_date) tuples
        property_date_pairs = [
            (item['property_number'], item['latest_date']) 
            for item in latest_by_property
        ]
        
        # Build Q filter to match these exact combinations
        q_filter = Q()
        for prop_num, latest_date in property_date_pairs:
            q_filter |= Q(property_number=prop_num, snapshotdate=latest_date)
        
        # Return filtered queryset with latest snapshot per property
        return filtered_qs.filter(q_filter)

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
        # Support multiple years via getlist (handle both QueryDict and dict)
        if hasattr(self.params, 'getlist'):
            period_years = self.params.getlist('period_year')
        else:
            py = self.params.get('period_year')
            period_years = [py] if py else []
        period_year = period_years[0] if period_years else ''
        
        # Support multiple quarters via getlist
        if hasattr(self.params, 'getlist'):
            period_quarters = self.params.getlist('period_quarter')
        else:
            pq = self.params.get('period_quarter')
            period_quarters = [pq] if pq else []
        period_quarter = period_quarters[0] if period_quarters else ''
        
        period_month = self.params.get('period_month') or ''
        legacy_period = self.params.get('period')
        if not period_mode and legacy_period:
            mode, val = self.infer_period_from_legacy(legacy_period)
            if mode == 'year':
                period_mode, period_year = 'year', val
                period_years = [val]
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
            # Use previous month by default (since current month data may be incomplete)
            default_period, default_mode = self._get_previous_month_period(sorted_periods)
            if default_period:
                selected_period = default_period
                period_mode = default_mode
                period_month = selected_period

        # Build base queryset and apply simple filters from params
        qs = OlympusLeaseTrendAnalysis.objects.all()
        # Support multi-select: params may contain arrays or single strings
        selected_investor = self.params.getlist('investor') if hasattr(self.params, 'getlist') else (
            self.params.get('investor', []) if isinstance(self.params.get('investor', ''), list) else 
            [self.params.get('investor', '')] if self.params.get('investor', '') else []
        )
        selected_regional_manager = self.params.getlist('regional_manager') if hasattr(self.params, 'getlist') else (
            self.params.get('regional_manager', []) if isinstance(self.params.get('regional_manager', ''), list) else 
            [self.params.get('regional_manager', '')] if self.params.get('regional_manager', '') else []
        )
        selected_community = self.params.getlist('community') if hasattr(self.params, 'getlist') else (
            self.params.get('community', []) if isinstance(self.params.get('community', ''), list) else 
            [self.params.get('community', '')] if self.params.get('community', '') else []
        )
        
        # Filter out empty strings
        selected_investor = [i for i in selected_investor if i]
        selected_regional_manager = [rm for rm in selected_regional_manager if rm]
        selected_community = [c for c in selected_community if c]
        
        # Apply filters using __in lookup for arrays
        if selected_investor:
            qs = qs.filter(investor__in=selected_investor)
        if selected_regional_manager:
            qs = qs.filter(regional_area_manager__in=selected_regional_manager)
        if selected_community:
            qs = qs.filter(property_name__in=selected_community)

        # Business rule: exclude BLACKSTONE/LIVCOR data for periods after June 2025.
        # This investor's data should only be used for periods up to and including June 2025.
        def _period_after_jun_2025(mode, year, month, quarter):
            try:
                if mode == 'month' and month:
                    # month is like 'Oct-2025' or 'Oct 2025'
                    import re
                    m = re.match(r'([A-Za-z]{3})[- ](\d{4})', month)
                    if m:
                        mon_abbr, yr = m.groups()
                        mon_num = list(__import__('calendar').month_abbr).index(mon_abbr)
                        yr = int(yr)
                        return (yr > 2025) or (yr == 2025 and mon_num > 6)
                if mode == 'quarter' and quarter:
                    # quarter like 'Q3-2025' or 'Q3-2025'
                    import re
                    mm = re.search(r'Q(\d)', quarter, re.I)
                    yy = re.search(r'(\d{4})', quarter)
                    if mm and yy:
                        qnum = int(mm.group(1))
                        yr = int(yy.group(1))
                        # Q3 begins in July
                        return (yr > 2025) or (yr == 2025 and qnum >= 3)
                if mode == 'year' and year and str(year).isdigit():
                    y = int(year)
                    return y > 2025
            except Exception:
                return False
            return False

        exclude_blackstone = _period_after_jun_2025(period_mode, period_year, period_month, period_quarter)
        # Apply exclusion when period is after Jun-2025 and the user did not
        # explicitly select BLACKSTONE/LIVCOR. If the user selected other
        # investors but not BLACKSTONE, still exclude BLACKSTONE rows.
        try:
            sel_up = [s.upper() for s in selected_investor] if selected_investor else []
        except Exception:
            sel_up = []
        if exclude_blackstone and 'BLACKSTONE/LIVCOR' not in sel_up:
            qs = qs.exclude(investor__iexact='BLACKSTONE/LIVCOR')

        # Determine the initial latest-month selection: prefer explicit period params, otherwise previous month
        if not (period_year or period_quarter or period_month or selected_period) and sorted_periods:
            # Use previous month by default (since current month data may be incomplete)
            default_period, default_mode = self._get_previous_month_period(sorted_periods)
            if default_period:
                selected_period = default_period
                period_mode = default_mode
                period_month = selected_period

        # Build a filtered queryset for the selected period (support multi-month selection)
        period_qs = qs
        if period_mode == 'month':
            # Support multi-month: getlist, fall back to single value
            period_months = []
            if hasattr(self.params, 'getlist'):
                period_months = self.params.getlist('period_month')
            if not period_months and period_month:
                period_months = [period_month]
            
            if period_months:
                # Build OR query across multiple selected months
                q_filter = Q()
                for pm in period_months:
                    mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', pm)
                    if mm:
                        month_name, year = mm.groups()
                        month_num = datetime.strptime(month_name, "%b").strftime("%m")
                        q_filter |= Q(snapshotdate__startswith=f"{year}-{month_num}") | Q(snapshotdate__icontains=f"{month_name}-{year}")
                if q_filter:
                    period_qs = qs.filter(q_filter)
        elif period_mode == 'quarter':
            # Support multi-quarter: getlist, fall back to single value
            if len(period_quarters) == 0 and period_quarter:
                period_quarters = [period_quarter]
            
            if period_quarters:
                # Build OR query across multiple selected quarters
                q_filter = Q()
                for pq in period_quarters:
                    parts = re.split('[-_]', pq)
                    qpart = parts[0].upper()
                    year = parts[-1]
                    qnum = int(re.sub('[^0-9]','', qpart))
                    start_month = (qnum-1)*3 + 1
                    months_nums = [ f"{m:02d}" for m in range(start_month, start_month+3) ]
                    for mnum in months_nums:
                        q_filter |= Q(snapshotdate__startswith=f"{year}-{mnum}")
                if q_filter:
                    period_qs = qs.filter(q_filter)
        elif period_mode == 'year' and period_year:
            # Support 'all' sentinel to indicate all available years
            if str(period_year).lower() == 'all':
                period_qs = qs
            else:
                # Support multiple years
                if len(period_years) > 1:
                    q_filter = Q()
                    for year in period_years:
                        q_filter |= Q(snapshotdate__startswith=f"{year}-") | Q(snapshotdate__endswith=f"-{year}")
                    period_qs = qs.filter(q_filter)
                else:
                    year = period_year
                    period_qs = qs.filter(Q(snapshotdate__startswith=f"{year}-") | Q(snapshotdate__endswith=f"-{year}"))

        # Narrow to last-day snapshot within selected period for KPI cards (dedupe per property)
        # If using the summary MV, compute KPI directly from it to avoid scanning large raw table
        use_summary = (period_mode == 'year' and str(period_year).lower() == 'all')
        
        # Check if multiple periods are selected
        has_multiple_periods = False
        if period_mode == 'month' and hasattr(self.params, 'getlist'):
            period_months_list = self.params.getlist('period_month')
            has_multiple_periods = len(period_months_list) > 1
        elif period_mode == 'quarter' and len(period_quarters) > 1:
            has_multiple_periods = True
        elif period_mode == 'year' and len(period_years) > 1:
            has_multiple_periods = True
        
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
            # When multiple periods are selected, we want to show ALL data from all periods
            # not just the latest per property. This allows users to see data across time.
            # For each period, we pick the last day snapshot, then combine all of them.
            if has_multiple_periods:
                # Get unique snapshot dates in the filtered data and group by month
                distinct_dates = period_qs.values_list('snapshotdate', flat=True).distinct().order_by('snapshotdate')
                
                # Group dates by year-month to get the last day of each month
                from collections import defaultdict
                dates_by_month = defaultdict(list)
                for d in distinct_dates:
                    if d:
                        key = (d.year, d.month)
                        dates_by_month[key].append(d)
                
                # Build Q filter for the latest date in each month
                q_filter = Q()
                for month_key, dates in dates_by_month.items():
                    latest_date = max(dates)
                    q_filter |= Q(snapshotdate=latest_date)
                
                last_day_qs = period_qs.filter(q_filter) if q_filter else period_qs.none()
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
              COALESCE(SUM(occupied_units),0) AS occupied_units
            FROM (
              SELECT DISTINCT ON (property_number) property_number, total_units, occupied_units, snapshotdate
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
                    # compute portfolio-weighted occupancy (occupied / total * 100)
                    if total_units:
                        avg_occupancy = round((occupied_units / total_units) * 100.0, 2)
                    else:
                        avg_occupancy = 0.0
        else:
            if last_day_qs.exists():
                # compute portfolio-weighted occupancy using sums of units
                agg = last_day_qs.aggregate(total_units_sum=Sum('total_units'), occupied_units_sum=Sum('occupied_units'))
                total_units = int(agg.get('total_units_sum') or 0)
                occupied_units = int(agg.get('occupied_units_sum') or 0)
                unique_props = last_day_qs.values('property_number', 'property_name').distinct()
                total_properties = unique_props.count()
                if total_units:
                    avg_occupancy = round((occupied_units / total_units) * 100.0, 2)
                else:
                    avg_occupancy = 0.0

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
        # Include additional metric fields so the client table can display
        # latest snapshot, occupancy%, rents, moves and application counts.
        
        # When multiple months are selected, show aggregated monthly data instead of snapshots
        if period_mode == 'month' and has_multiple_periods:
            # Build aggregated monthly data from period_qs (all days in selected months)
            from django.db.models import Avg, Max
            from django.db.models.functions import ExtractYear, ExtractMonth
            import calendar as month_calendar
            
            # Group by property and month, then aggregate
            prop_values_raw = (period_qs
                .annotate(year=ExtractYear('snapshotdate'), month=ExtractMonth('snapshotdate'))
                .values('property_number', 'property_name', 'investor', 'regional_area_manager',
                       'regional_director', 'asst_manager', 'year', 'month')
                .annotate(
                    total_units=Max('total_units'),  # Units typically constant per property
                    occupied_units=Avg('occupied_units'),  # Average occupancy across the month
                    percentage_occupacy=Avg('percentage_occupacy'),  # Average occupancy %
                    effective_rent=Avg('effective_rent'),  # Average effective rent
                    market_rent=Avg('market_rent'),  # Average market rent
                    total_move_ins=Sum('total_move_ins'),  # Sum of move-ins across all days
                    total_move_outs=Sum('total_move_outs'),  # Sum of move-outs across all days
                    applications=Sum('applications'),  # Sum of applications
                    avg_amount_per_sqft=Avg('avg_amount_per_sqft'),  # Average $/sqft
                    snapshotdate=Max('snapshotdate')  # Last snapshot date in the month for reference
                )
                .order_by('property_number', '-year', '-month'))
            
            # Convert to list and format the data
            prop_values = []
            for row in prop_values_raw:
                # Create a period label like "Jan-2025"
                month_name = month_calendar.month_abbr[row['month']]
                period_label = f"{month_name}-{row['year']}"
                
                # Round numeric values for display
                prop_values.append({
                    'property_number': row['property_number'],
                    'property_name': row['property_name'],
                    'investor': row['investor'],
                    'regional_area_manager': row['regional_area_manager'],
                    'regional_director': row['regional_director'],
                    'asst_manager': row['asst_manager'],
                    'total_units': int(row['total_units'] or 0),
                    'occupied_units': round(row['occupied_units'] or 0, 1),  # Show avg as decimal
                    'percentage_occupacy': round(row['percentage_occupacy'] or 0, 2),
                    'effective_rent': round(row['effective_rent'] or 0, 2),
                    'market_rent': round(row['market_rent'] or 0, 2),
                    'total_move_ins': int(row['total_move_ins'] or 0),
                    'total_move_outs': int(row['total_move_outs'] or 0),
                    'applications': int(row['applications'] or 0),
                    'avg_amount_per_sqft': round(row['avg_amount_per_sqft'] or 0, 2),
                    'snapshotdate': row['snapshotdate'],  # Reference date
                    'period_label': period_label  # Add period label for display
                })
        else:
            # Single period or non-month mode: use snapshot data as before
            prop_values = last_day_qs.values(
                'property_number', 'property_name', 'total_units', 'investor', 'regional_area_manager',
                'regional_director', 'asst_manager', 'occupied_units',
                'snapshotdate', 'percentage_occupacy', 'effective_rent', 'market_rent',
                'total_move_ins', 'total_move_outs', 'applications', 'avg_amount_per_sqft'
            ).distinct()
            prop_values = list(prop_values)
        
        # Use in-memory paginator so view can render `properties` as page object
        paginator = Paginator(prop_values, 10)
        page = int(self.params.get('page', 1)) if str(self.params.get('page','1')).isdigit() else 1
        properties_page = paginator.get_page(page)

        # Build a full (non-paginated) properties list used by modals (contains dicts
        # with property_name and total_units). Using prop_values ensures this list
        # reflects the same latest-per-property rows used for the table/pagination.
        try:
            prop_list = list(prop_values)
            # --- Aggregate per-property move-out totals from moveout reasons table ---
            try:
                from dashboard.models import OlympusLeaseMoveoutReasonsTrendMonthly
                # use top-level Sum import (avoid local import which makes Sum a local name)
                # Determine selected year/months from the selected period so we query the same buckets
                sel_period = selected_period or (period_month or period_quarter or period_year)
                sel_mode = period_mode
                mo_qs = OlympusLeaseMoveoutReasonsTrendMonthly.objects.all()
                # Apply same filters as for properties
                if selected_investor:
                    mo_qs = mo_qs.filter(investor=selected_investor)
                if selected_regional_manager:
                    mo_qs = mo_qs.filter(regional_area_manager=selected_regional_manager)
                if selected_community:
                    mo_qs = mo_qs.filter(property_name=selected_community)

                # Narrow by selected period: if month(s) available, filter by those month numbers and year
                def _period_to_year_months(mode, month_val, quarter_val, year_val):
                    import calendar as _cal
                    if mode == 'month' and month_val:
                        import re
                        m = re.match(r'([A-Za-z]{3})[- ](\d{4})', month_val)
                        if m:
                            mon_abbr, yy = m.groups()
                            mn = list(_cal.month_abbr).index(mon_abbr)
                            return int(yy), [mn]
                    if mode == 'quarter' and quarter_val:
                        import re
                        qm = re.search(r'Q(\d)', quarter_val, re.I)
                        yy = re.search(r'(\d{4})', quarter_val)
                        if qm and yy:
                            qnum = int(qm.group(1))
                            y = int(yy.group(1))
                            start = (qnum-1)*3 + 1
                            months = [m for m in range(start, start+3)]
                            return y, months
                    if mode == 'year' and year_val and str(year_val).isdigit():
                        return int(year_val), list(range(1,13))
                    return None, None

                year_for_mo, months_for_mo = _period_to_year_months(sel_mode, period_month, period_quarter, period_year)
                if year_for_mo and months_for_mo:
                    mo_qs = mo_qs.filter(enddateofmonth__year=year_for_mo, enddateofmonth__month__in=months_for_mo)

                # Aggregate per property
                mo_agg = mo_qs.values('property_number').annotate(total_move_outs_sum=Sum('move_out_count'))
                mo_map = {r['property_number']: int(r['total_move_outs_sum'] or 0) for r in mo_agg}
            except Exception:
                mo_map = {}
            
            # Fetch delinquency data for the selected period
            delinq_map = {}
            exposure_map = {}
            kpi_delinquency = None
            kpi_exposure = None
            try:
                # Parse the selected period to determine which month's KPI data to fetch
                kpi_qs = OlympusLeaseKpisTrendMonthly.objects.all()
                
                # Determine the target month based on period selection
                if period_mode == 'month' and period_month:
                    # Parse "Oct-2025" format
                    mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', period_month)
                    if mm:
                        mon_abbr, yr = mm.groups()
                        try:
                            mon_num = list(calendar.month_abbr).index(mon_abbr)
                            yr = int(yr)
                            # Get the last day of the selected month
                            last_day = calendar.monthrange(yr, mon_num)[1]
                            target_date = date(yr, mon_num, last_day)
                            kpi_qs = kpi_qs.filter(enddateofmonth=target_date)
                        except (ValueError, IndexError):
                            pass
                
                # Apply community filter if present
                if selected_community:
                    kpi_qs = kpi_qs.filter(property_name__in=selected_community)
                
                # Aggregate delinquency and exposure per property
                kpi_agg = kpi_qs.values('property_number', 'delinquency', 'exposure')
                for r in kpi_agg:
                    prop_num = r['property_number']
                    # Convert decimal values to percentages (multiply by 100)
                    if r['delinquency'] is not None:
                        delinq_map[prop_num] = r['delinquency'] * 100
                    if r['exposure'] is not None:
                        exposure_map[prop_num] = r['exposure'] * 100
                
                # Calculate average delinquency and exposure for KPI cards
                if delinq_map:
                    kpi_delinquency = sum(delinq_map.values()) / len(delinq_map)
                if exposure_map:
                    kpi_exposure = sum(exposure_map.values()) / len(exposure_map)
            except Exception as e:
                import traceback
                print(f"❌ ERROR fetching delinquency: {e}")
                traceback.print_exc()
                delinq_map = {}
                exposure_map = {}
                kpi_delinquency = None
                kpi_exposure = None
            
            # Add delinquency and exposure to KPI dict
            if kpi_delinquency is not None:
                kpi['delinquency'] = kpi_delinquency
            if kpi_exposure is not None:
                kpi['exposure'] = kpi_exposure
            
            # Include additional fields so modals and client-side JSON have
            # investor / manager / director / asst_manager / occupied_units and metric columns
            properties_full = sorted([
                {
                    'property_number': r.get('property_number'),
                    'property_name': r.get('property_name'),
                    'total_units': int(r.get('total_units') or 0),
                    'occupied_units': int(r.get('occupied_units') or 0),
                    'investor': r.get('investor') or '',
                    'regional_area_manager': r.get('regional_area_manager') or '',
                    'regional_director': r.get('regional_director') or '',
                    'asst_manager': r.get('asst_manager') or '',
                    # metrics that may be present on summary rows or raw rows
                    'latest_snapshot_date': r.get('snapshotdate') or r.get('latest_snapshot_date') or None,
                    'occupancy_pct': (r.get('occupancy_pct') if r.get('occupancy_pct') is not None else r.get('percentage_occupacy')),
                    'effective_rent': r.get('effective_rent'),
                    'market_rent': r.get('market_rent'),
                    'total_move_ins': r.get('total_move_ins') or 0,
                    # prefer aggregated move-out totals from the moveout reasons table when available
                    'total_move_outs': (mo_map.get(r.get('property_number')) if mo_map.get(r.get('property_number')) is not None else (r.get('total_move_outs') or 0)),
                    'applications': r.get('applications') or 0,
                    'avg_amount_per_sqft': r.get('avg_amount_per_sqft'),
                    # Add delinquency and exposure data for the selected period (already in percentage form)
                    'delinquency': delinq_map.get(r.get('property_number')),
                    'exposure': exposure_map.get(r.get('property_number'))
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

        # Determine if we should use weekly bucketing: only when exactly ONE month is selected
        selected_months_count = 0
        if period_mode == 'month':
            if hasattr(self.params, 'getlist'):
                selected_months_list = self.params.getlist('period_month')
                selected_months_count = len([m for m in selected_months_list if m])
            if selected_months_count == 0 and period_month:
                selected_months_count = 1

        use_weekly_bucketing = (period_mode == 'month' and selected_months_count == 1)

        if use_weekly_bucketing:
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

            # Do NOT pre-initialize buckets - only create them when we have actual data
            # This ensures we only show week groups that have data available
            
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
        units_series = []  # Track total units per bucket for occupancy chart

        for key in sorted(buckets.keys()):
            bucket = buckets[key]
            if use_weekly_bucketing:
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
            units_sum = 0  # Sum total units for this bucket
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
                # accumulate total units for this property
                try:
                    units_sum += int(row.get('total_units') or 0)
                except Exception:
                    pass

            eff_series.append(round(sum(eff_vals) / len(eff_vals), 2) if eff_vals else 0)
            mkt_series.append(round(sum(mkt_vals) / len(mkt_vals), 2) if mkt_vals else 0)
            # compute portfolio-weighted occupancy for the bucket using occupied_units/total_units
            try:
                total_units_bucket = sum(int(row.get('total_units') or 0) for (sd, row) in bucket.values())
                occupied_units_bucket = sum(int(row.get('occupied_units') or 0) for (sd, row) in bucket.values())
                if total_units_bucket:
                    occ_val = round((occupied_units_bucket / total_units_bucket) * 100.0, 2)
                else:
                    occ_val = 0
            except Exception:
                occ_val = round(sum(occ_vals) / len(occ_vals), 2) if occ_vals else 0
            occ_series.append(occ_val)
            rev_series.append(round(rev_sum / 1000.0, 2))  # k$
            units_series.append(units_sum)  # Total units for this time period

        chart_rent = {
            'labels': labels,
            'datasets': [
                {'label': 'Effective', 'data': eff_series, 'backgroundColor': '#0E555A', 'borderColor': '#0E555A'},
                {'label': 'Market', 'data': mkt_series, 'backgroundColor': '#C69A58', 'borderColor': '#C69A58'},
            ]
        }

        chart_occrev = {
            'labels': labels,
            'datasets': [
                {'type': 'bar', 'label': 'Occupancy %', 'yAxisID': 'yL', 'data': occ_series, 'backgroundColor': '#0E555A', 'borderColor': '#0E555A'},
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

        # Correlation chart: combine occupancy, rent, and units for pattern analysis
        chart_correlation = {
            'labels': labels,
            'datasets': [
                {
                    'type': 'line',
                    'label': 'Occupancy %',
                    'yAxisID': 'yOccupancy',
                    'data': occ_series,
                    'borderColor': '#0E555A',
                    'backgroundColor': 'rgba(14,85,90,0.1)',
                    'borderWidth': 2,
                    'tension': 0.3,
                    'fill': False,
                    'pointRadius': 4,
                    'pointHoverRadius': 6
                },
                {
                    'type': 'line',
                    'label': 'Avg Effective Rent',
                    'yAxisID': 'yRent',
                    'data': eff_series,
                    'borderColor': '#C69A58',
                    'backgroundColor': 'rgba(198,154,88,0.1)',
                    'borderWidth': 2,
                    'tension': 0.3,
                    'fill': False,
                    'pointRadius': 4,
                    'pointHoverRadius': 6
                },
                {
                    'type': 'line',
                    'label': 'Total Units',
                    'yAxisID': 'yUnits',
                    'data': units_series,
                    'borderColor': '#10B981',
                    'backgroundColor': 'rgba(16,185,129,0.1)',
                    'borderWidth': 2,
                    'tension': 0.3,
                    'fill': False,
                    'pointRadius': 4,
                    'pointHoverRadius': 6
                }
            ]
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

        # Create a comprehensive period label for display
        period_display_label = selected_period
        if period_mode == 'quarter' and len(period_quarters) > 1:
            # Show all selected quarters
            period_display_label = ', '.join(sorted(period_quarters, reverse=True))
        elif period_mode == 'year' and len(period_years) > 1:
            # Show all selected years
            period_display_label = ', '.join(sorted([str(y) for y in period_years], reverse=True))
        elif period_mode == 'month' and period_months:
            # For multiple months, show range or list
            if len(period_months) > 3:
                period_display_label = f"{len(period_months)} months selected"
            else:
                period_display_label = ', '.join(period_months)

        # Compose final context returned to view/template
        return {
            'periods': sorted_periods,
            'period_mode': period_mode,
            'period_year': period_year,
            'period_years': period_years,  # List of selected years for multi-year support
            'period_quarter': period_quarter,
            'period_quarters': period_quarters,  # List of selected quarters for multi-quarter support
            'period_month': period_month,
            'selected_period': selected_period,
            'period_display_label': period_display_label,  # Comprehensive label showing all selected periods
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
            'chart_correlation': chart_correlation,
            'kpi_snapshot_date': kpi_snapshot_date,
        }



