from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Sum, Avg, Count, Q, F
from .models import OlympusLeaseTrendAnalysis
from datetime import datetime
import json
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.template.loader import render_to_string
import re

def parse_period(period):
	# period: 'Jun-2025', '2025', '2025-Q1', etc.
	if re.match(r'\d{4}-Q[1-4]', period):
		year, q = period.split('-Q')
		q = int(q)
		months = {
			1: ['Jan', 'Feb', 'Mar'],
			2: ['Apr', 'May', 'Jun'],
			3: ['Jul', 'Aug', 'Sep'],
			4: ['Oct', 'Nov', 'Dec']
		}[q]
		return year, months
	elif re.match(r'\d{4}', period):
		return period, None
	elif re.match(r'[A-Za-z]{3}-\d{4}', period):
		m, y = period.split('-')
		return y, [m]
	return None, None

@login_required
def dashboard(request):
	user = request.user
	# Get available periods for dropdown (hierarchical: years > quarters > months)
	all_dates = OlympusLeaseTrendAnalysis.objects.values_list('snapshotdate', flat=True)
	periods = {}
	for d in all_dates:
		m1 = re.match(r'(\d{4})[-/](\d{2})[-/](\d{2})', d)
		if m1:
			year = m1.group(1)
			month = m1.group(2)
			month_name = datetime.strptime(month, "%m").strftime("%b")
			periods.setdefault(year, {"months": set(), "quarters": {}})
			periods[year]["months"].add(month_name)
			# Assign month to quarter
			q = (int(month)-1)//3 + 1
			periods[year]["quarters"].setdefault(f"Q{q}", set()).add(month_name)
		else:
			m2 = re.match(r'([A-Za-z]{3})-(\d{4})', d)
			if m2:
				month_name, year = m2.groups()
				periods.setdefault(year, {"months": set(), "quarters": {}})
				periods[year]["months"].add(month_name)
				q = (datetime.strptime(month_name, "%b").month-1)//3 + 1
				periods[year]["quarters"].setdefault(f"Q{q}", set()).add(month_name)
	# Sort years descending, months/quarters ascending
	sorted_periods = {}
	for year in sorted(periods.keys(), reverse=True):
		sorted_periods[year] = {
			"months": sorted(periods[year]["months"], key=lambda m: datetime.strptime(m, "%b")),
			"quarters": {q: sorted(periods[year]["quarters"][q], key=lambda m: datetime.strptime(m, "%b")) for q in sorted(periods[year]["quarters"].keys())}
		}
	# Read period mode and explicit params (new API)
	period_mode = request.GET.get('period_mode') or ''
	period_year = request.GET.get('period_year') or ''
	period_quarter = request.GET.get('period_quarter') or ''
	period_month = request.GET.get('period_month') or ''
	# Backwards compat: if legacy `period` provided but mode not set, try to infer
	legacy_period = request.GET.get('period')
	if not period_mode and legacy_period:
		# crude inference
		if re.match(r'^\d{4}$', legacy_period):
			period_mode = 'year'; period_year = legacy_period
		elif re.match(r'^[A-Za-z]{3}-\d{4}$', legacy_period) or re.match(r'^[A-Za-z]{3} \d{4}$', legacy_period):
			period_mode = 'month'; period_month = legacy_period
		elif re.match(r'^Q\d[-_]?\d{4}$', legacy_period, re.I):
			period_mode = 'quarter'; period_quarter = legacy_period

	# If still no explicit mode, default to month (existing behavior)
	if not period_mode:
		period_mode = 'month'

	# If no specific period provided, choose latest available month as before
	selected_period = legacy_period
	if not (period_year or period_quarter or period_month or selected_period) and sorted_periods:
		latest_year = next(iter(sorted(sorted_periods.keys(), reverse=True)))
		latest_quarter = next(iter(sorted(sorted_periods[latest_year]['quarters'].keys(), reverse=True)))
		latest_month = sorted(sorted_periods[latest_year]['quarters'][latest_quarter], key=lambda m: datetime.strptime(m, "%b"))[-1]
		selected_period = f"{latest_month}-{latest_year}"
		period_mode = 'month'; period_month = selected_period

	qs = OlympusLeaseTrendAnalysis.objects.all()
	# Apply additional filters (investor, regional_manager, community) early so snapshot selection respects them
	selected_investor = request.GET.get('investor','')
	selected_regional_manager = request.GET.get('regional_manager','')
	selected_community = request.GET.get('community','')
	if selected_investor:
		qs = qs.filter(investor=selected_investor)
	if selected_regional_manager:
		qs = qs.filter(regional_area_manager=selected_regional_manager)
	if selected_community:
		qs = qs.filter(property_name=selected_community)

	last_day_qs = qs.none()

	# Helpers
	month_num_map = {1:'01',2:'02',3:'03',4:'04',5:'05',6:'06',7:'07',8:'08',9:'09',10:'10',11:'11',12:'12'}
	month_name_map = {1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'}

	def pick_last_day_from_qs(filtered_qs):
		if not filtered_qs.exists():
			return qs.none()
		max_date = filtered_qs.aggregate(max_date=Max('snapshotdate'))['max_date']
		return filtered_qs.filter(snapshotdate=max_date) if max_date else qs.none()

	# Build filtered queryset according to mode
	if period_mode == 'year' and period_year:
		# Match both 'YYYY-MM-DD...' and 'Mon-YYYY' patterns
		year = period_year
		year_q = (Q(snapshotdate__startswith=f"{year}-") | Q(snapshotdate__endswith=f"-{year}"))
		year_qs = qs.filter(year_q)
		last_day_qs = pick_last_day_from_qs(year_qs)
		# set selected_period to latest day repr if not provided
		if last_day_qs.exists() and not selected_period:
			selected_period = last_day_qs.first().snapshotdate

	elif period_mode == 'quarter' and period_quarter:
		# period_quarter expected like 'Q1-2024' or 'Q1_2024'
		parts = re.split('[-_]', period_quarter)
		qpart = parts[0].upper()
		year = parts[-1]
		qnum = int(re.sub('[^0-9]','', qpart))
		# months for quarter
		start_month = (qnum-1)*3 + 1
		months_nums = [ month_num_map[m] for m in range(start_month, start_month+3) ]
		months_names = [ month_name_map[m] for m in range(start_month, start_month+3) ]
		q_filter = Q()
		for mnum, mname in zip(months_nums, months_names):
			q_filter |= Q(snapshotdate__startswith=f"{year}-{mnum}") | Q(snapshotdate__icontains=f"{mname}-{year}")
		quarter_qs = qs.filter(q_filter)
		last_day_qs = pick_last_day_from_qs(quarter_qs)
		if last_day_qs.exists() and not selected_period:
			selected_period = last_day_qs.first().snapshotdate

	else:
		# Default: month selection. Consider explicit period_month, or selected_period, or period_year fallback
		month_sel = period_month or selected_period
		m = None
		if month_sel:
			# normalize forms like 'Jan-2024' or 'Jan- 2024'
			mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', month_sel)
			if mm:
				month_name, year = mm.groups()
				month_num = datetime.strptime(month_name, "%b").strftime("%m")
				month_str = f"{year}-{month_num}"
				month_qs = qs.filter(Q(snapshotdate__startswith=month_str) | Q(snapshotdate__icontains=f"{month_name}-{year}"))
				last_day_qs = pick_last_day_from_qs(month_qs)
				selected_period = f"{month_name}-{year}"
		# If still empty and period_year provided, try to pick last day in the year
		if not last_day_qs.exists() and period_year:
			year = period_year
			year_qs = qs.filter(Q(snapshotdate__startswith=f"{year}-") | Q(snapshotdate__endswith=f"-{year}"))
			last_day_qs = pick_last_day_from_qs(year_qs)

	# If still no data, leave last_day_qs empty

	# Compute KPIs and table from last_day_qs
	total_properties = 0
	total_units = 0
	all_properties = []
	occupancy = 0
	if last_day_qs.exists():
		unique_properties = last_day_qs.values('property_number', 'property_name', 'total_units').distinct()
		total_properties = unique_properties.count()
		total_units = sum([row['total_units'] for row in unique_properties])
		all_properties = list(unique_properties)
		occupancy = last_day_qs.aggregate(occ=Avg('percentage_occupacy'))['occ'] or 0

	# Table data (paginated)
	property_list = last_day_qs.values(
		'property_number',
		'property_name',
		'total_units',
		'investor',
		'regional_area_manager',
		'regional_director',
		'asst_manager',
		'occupied_units'
	).distinct()
	paginator = Paginator(list(property_list), 10)
	page_number = request.GET.get('page')
	properties = paginator.get_page(page_number)

	kpi = {
		'total_properties': total_properties,
		'total_units': total_units,
		'occupancy': f"{occupancy:.2f}%",
		'exposure': 0,
		'delinquency': 0,
		'avg_turn_time': 0,
		'service_requests': 0,
		'google_stars': 0,
		'renewal_conversion': 0,
	}

	# Build dependent maps for client-side filtering
	inv_to_regional_map = {}
	inv_reg_to_communities_map = {}
	for row in OlympusLeaseTrendAnalysis.objects.values('investor','regional_area_manager','property_name'):
		inv = row.get('investor') or ''
		rm = row.get('regional_area_manager') or ''
		prop = row.get('property_name') or ''
		inv_to_regional_map.setdefault(inv, set()).add(rm)
		inv_reg_to_communities_map.setdefault((inv,rm), set()).add(prop)
	# Convert sets to sorted lists for JSON-safe context
	inv_to_regional = {k: sorted(list(v)) for k,v in inv_to_regional_map.items()}
	inv_reg_to_communities = {f"{k[0]}|||{k[1]}": sorted(list(v)) for k,v in inv_reg_to_communities_map.items()}

	# Serialize dependent maps as JSON strings for safe insertion into JS
	inv_to_regional_json = json.dumps(inv_to_regional)
	inv_reg_to_communities_json = json.dumps(inv_reg_to_communities)

	# Build context for template rendering
	context = {
		'user': user,
		'properties': properties,
		'kpi': kpi,
		'period_options': sorted_periods,
		'selected_period': selected_period,
		'selected_year': period_year,
		'selected_quarter': period_quarter,
		'selected_month': period_month,
		'selected_period_mode': period_mode,
		'all_properties': all_properties,
		# Provide filter lists
		'investors': OlympusLeaseTrendAnalysis.objects.values_list('investor', flat=True).distinct().order_by('investor'),
		'regional_managers': OlympusLeaseTrendAnalysis.objects.values_list('regional_area_manager', flat=True).distinct().order_by('regional_area_manager'),
		'communities': OlympusLeaseTrendAnalysis.objects.values_list('property_name', flat=True).distinct().order_by('property_name'),
		'inv_to_regional': inv_to_regional,
		'inv_reg_to_communities': inv_reg_to_communities,
		'inv_to_regional_json': inv_to_regional_json,
		'inv_reg_to_communities_json': inv_reg_to_communities_json,
		'selected_investor': request.GET.get('investor',''),
		'selected_regional_manager': request.GET.get('regional_manager',''),
		'selected_community': request.GET.get('community',''),
	}

	# If this is an AJAX fragment request, return rendered fragments as JSON
	is_xhr = request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest' or request.headers.get('x-requested-with') == 'XMLHttpRequest'
	if is_xhr:
		kpi_html = render_to_string('dashboard/partials/_kpi_cards.html', context=context, request=request)
		table_html = render_to_string('dashboard/partials/_property_table.html', context=context, request=request)
		return JsonResponse({'kpi_html': kpi_html, 'table_html': table_html, 'selected_period': selected_period})

	return render(request, 'dashboard/dashboard.html', context)
