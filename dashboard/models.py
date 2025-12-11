from django.db import models



class PropertyMaster(models.Model):
	property_number = models.IntegerField(primary_key=True)
	property_name = models.TextField(blank=True, null=True)

	class Meta:
		db_table = 'property_master'



class OlympusLeaseTrendAnalysis(models.Model):
	# Mirror of existing DB table `olympus_lease_trend_analysis`.
	# The actual table uses a composite key (snapshot_date, property_number).
	# Django doesn't support composite primary keys; keep managed=False and
	# enforce uniqueness via unique_together to match the DB semantics.
	snapshotdate = models.DateField(db_column="snapshot_date")
	property_number = models.IntegerField()
	property_name = models.CharField(max_length=70, blank=True, null=True)
	investor = models.CharField(max_length=100, blank=True, null=True)
	regional_area_manager = models.CharField(max_length=100, blank=True, null=True)
	regional_director = models.CharField(max_length=100, blank=True, null=True)
	senior_regional = models.CharField(max_length=100, blank=True, null=True)
	asst_manager = models.CharField(max_length=100, blank=True, null=True)
	total_units = models.IntegerField(blank=True, null=True)
	occupied_units = models.IntegerField(blank=True, null=True)
	percentage_occupacy = models.FloatField(blank=True, null=True)
	total_move_ins = models.IntegerField(blank=True, null=True)
	total_move_outs = models.IntegerField(blank=True, null=True)
	avg_amount_per_sqft = models.FloatField(blank=True, null=True)
	average_rent_move_ins = models.FloatField(blank=True, null=True)
	average_rent_move_outs = models.FloatField(blank=True, null=True)
	effective_rent = models.FloatField(blank=True, null=True)
	market_rent = models.FloatField(blank=True, null=True)
	eff_rent_modified_date = models.DateTimeField(blank=True, null=True)
	visit = models.IntegerField(blank=True, null=True)
	applications = models.IntegerField(blank=True, null=True)
	cancelled = models.IntegerField(blank=True, null=True)
	denied = models.IntegerField(blank=True, null=True)
	vacant_pre_leased = models.IntegerField(blank=True, null=True)
	occupied_pre_leased = models.IntegerField(blank=True, null=True)
	ntv = models.IntegerField(db_column='NTV', blank=True, null=True)
	make_ready = models.IntegerField(blank=True, null=True)
	mtm = models.IntegerField(db_column='MTM', blank=True, null=True)
	current_month_expiring = models.IntegerField(blank=True, null=True)
	next_month_expiring = models.IntegerField(blank=True, null=True)
	month_after_next_expiring = models.IntegerField(blank=True, null=True)
	tbd_90_days = models.IntegerField(blank=True, null=True)
	down = models.IntegerField(blank=True, null=True)
	vacant_units_without_down_admin = models.IntegerField(blank=True, null=True)

	class Meta:
		managed = False
		db_table = 'olympus_lease_trend_analysis'
		unique_together = (('snapshotdate', 'property_number'),)


class LeaseTrendSummary(models.Model):
	"""Unmanaged model backed by a materialized view `web_ai.lease_trend_summary`.

	One row per property per year (or per property-year bucket). Fields mirror the
	aggregates used by the dashboard service.
	"""
	# Fields reflect the columns observed on the materialized view in the DB.
	property_number = models.IntegerField()
	property_name = models.CharField(max_length=70, blank=True, null=True)
	year = models.IntegerField()
	snapshotdate = models.DateField(blank=True, null=True)
	# Existing MV in DB exposed these column names (effective_rent etc.)
	effective_rent = models.FloatField(blank=True, null=True)
	market_rent = models.FloatField(blank=True, null=True)
	occupied_units = models.IntegerField(blank=True, null=True)
	total_units = models.IntegerField(blank=True, null=True)
	percentage_occupacy = models.FloatField(blank=True, null=True)
	investor = models.CharField(max_length=255, blank=True, null=True)
	regional_area_manager = models.CharField(max_length=100, blank=True, null=True)
	regional_director = models.CharField(max_length=100, blank=True, null=True)
	asst_manager = models.CharField(max_length=100, blank=True, null=True)

	class Meta:
		managed = False
		# Use a quoted schema-qualified table name so Django issues queries like:
		# SELECT ... FROM "web_ai"."lease_trend_summary"
		db_table = '"web_ai"."lease_trend_summary"'


class OlympusLeaseKpisTrendMonthly(models.Model):
	"""Unmanaged mapping for the monthly KPIs table used by the dashboard.

	This mirrors the Postgres table `web_ai.olympus_lease_kpis_trend_monthly` and
	is intended for read-only access (managed = False).
	"""
	property_name = models.CharField(max_length=255, blank=True, null=True)
	investor = models.CharField(max_length=255, blank=True, null=True)
	regional_area_manager = models.CharField(max_length=255, blank=True, null=True)
	regional_director = models.CharField(max_length=255, blank=True, null=True)
	senior_regional = models.CharField(max_length=255, blank=True, null=True)
	asst_manager = models.CharField(max_length=255, blank=True, null=True)
	property_number = models.IntegerField()
	enddateofmonth = models.DateField()
	kpi_month = models.CharField(max_length=20, blank=True, null=True)
	exposure = models.FloatField(blank=True, null=True)
	delinquency = models.FloatField(blank=True, null=True)
	average_turn_time = models.IntegerField(blank=True, null=True)
	service_request = models.IntegerField(blank=True, null=True)
	renewel_conversion = models.FloatField(blank=True, null=True)
	expirations = models.IntegerField(blank=True, null=True)
	renewed = models.IntegerField(blank=True, null=True)
	controllable_expense = models.BigIntegerField(blank=True, null=True)
	total_operating_expense = models.BigIntegerField(blank=True, null=True)
	# Column name contains a percent sign in the DB; map to a safer Python attribute
	rent_renewal_increase_pct = models.FloatField(db_column='rent_renewal_increase_%', blank=True, null=True)
	non_controllable_expense = models.BigIntegerField(blank=True, null=True)

	class Meta:
		managed = False
		db_table = '"web_ai"."olympus_lease_kpis_trend_monthly"'


class OlympusLeaseMoveoutReasonsTrendMonthly(models.Model):
	"""Unmanaged mapping for the move-out reasons monthly table.

	Mirror of Postgres table: web_ai.olympus_lease_moveout_reasons_trend_monthly
	Columns were introspected from the DB and mapped to safe Django fields.
	"""
	property_name = models.CharField(max_length=70, blank=True, null=True)
	investor = models.CharField(max_length=255, blank=True, null=True)
	regional_area_manager = models.CharField(max_length=100, blank=True, null=True)
	regional_director = models.CharField(max_length=100, blank=True, null=True)
	senior_regional = models.CharField(max_length=100, blank=True, null=True)
	asst_manager = models.CharField(max_length=100, blank=True, null=True)
	# moveout_category is NOT NULL in the DB
	moveout_category = models.CharField(max_length=50)
	enddateofmonth = models.DateField()
	property_number = models.IntegerField()
	# large/kpi-style text field; DB max length observed as 4000
	kpi_month = models.CharField(max_length=4000, blank=True, null=True)
	move_out_count = models.IntegerField(blank=True, null=True)

	class Meta:
		managed = False
		db_table = '"web_ai"."olympus_lease_moveout_reasons_trend_monthly"'


class CardDrillthrough(models.Model):
	"""Unmanaged model for web_ai.card_drillthrough materialized view.
	
	This view contains financial income/budget drill-through data with 
	category hierarchies and property-level metrics. Used for detailed
	financial reporting and analysis.
	
	Total rows: ~7.8M records
	
	Note: This is a materialized view without a primary key. 
	Use .values() or .values_list() queries for best results.
	Direct model instance access may have limitations.
	"""
	# Use ctid (PostgreSQL's physical row identifier) as a pseudo-primary key
	# This allows Django to work with the model but doesn't represent a real constraint
	ctid = models.TextField(primary_key=True, db_column='ctid')
	
	property_id = models.IntegerField(blank=True, null=True)
	category_id = models.IntegerField(blank=True, null=True)
	entity_id = models.IntegerField(blank=True, null=True)
	property_name = models.CharField(max_length=70, blank=True, null=True)
	category_name = models.CharField(max_length=255, blank=True, null=True)
	investor = models.CharField(max_length=100, blank=True, null=True)
	regional_area_manager = models.CharField(max_length=100, blank=True, null=True)
	# Column name contains special characters; use db_column to map it
	regional_vp_sr_vp = models.CharField(max_length=100, blank=True, null=True, db_column='Regional VP  | Sr. VP')
	community = models.TextField(blank=True, null=True)
	parent_category_name = models.TextField(blank=True, null=True)
	sub_category_name = models.CharField(max_length=255, blank=True, null=True)
	sub_sub_category_name = models.CharField(max_length=255, blank=True, null=True)
	month_end_date = models.DateTimeField(blank=True, null=True)
	# Income values are PostgreSQL 'money' type - Django will handle as decimal/string
	income_values = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	total_units = models.IntegerField(blank=True, null=True)
	income_values_all = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	income_values_per_unit_all = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	solddate_modified = models.DateTimeField(blank=True, null=True)
	acquisition_date = models.DateField(blank=True, null=True)

	class Meta:
		managed = False
		db_table = '"web_ai"."card_drillthrough"'


class FinanceKpiScorecard(models.Model):
	"""Unmanaged materialized view for finance KPI scorecard metrics."""
	property_id = models.IntegerField(primary_key=True)
	community = models.TextField(blank=True, null=True)
	regional_vp_sr_vp = models.CharField(max_length=255, blank=True, null=True, db_column='Regional VP  | Sr. VP')
	regional_area_manager = models.CharField(max_length=255, blank=True, null=True)
	investor = models.CharField(max_length=255, blank=True, null=True)
	month_end_date = models.DateField(blank=True, null=True)
	current_month = models.CharField(max_length=32, blank=True, null=True)
	actual = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	period_pct_month = models.FloatField(blank=True, null=True)
	period_pct_quarter = models.FloatField(blank=True, null=True)
	period_pct_year = models.FloatField(blank=True, null=True)
	sum_income_value_per_unit = models.FloatField(blank=True, null=True)
	yoy_operating_revenue = models.FloatField(blank=True, null=True)
	yoy_operating_expense = models.FloatField(blank=True, null=True)
	noi_percent_revenue = models.FloatField(blank=True, null=True, db_column='noi_as_%_of_revenue')
	executed_rent_yoy = models.FloatField(blank=True, null=True, db_column='executed_rent_yoy_percent')
	in_place_rent_per_sqft = models.FloatField(blank=True, null=True)

	class Meta:
		managed = False
		db_table = '"web_ai"."finance_kpi_scorecard"'


class TotalUnitOccupancyDrillThrough(models.Model):
	"""Unmanaged materialized view for unit occupancy drill-through data."""
	# Primary identifier columns
	property_name = models.TextField(blank=True, null=True)
	property_number = models.IntegerField(blank=True, null=True)
	unit = models.TextField(blank=True, null=True)
	unit_identifier = models.TextField(blank=True, null=True)
	onesite_id = models.TextField(blank=True, null=True)
	
	# Unit details
	unit_condition = models.TextField(blank=True, null=True)
	floor_plan = models.TextField(blank=True, null=True)
	beds_baths = models.TextField(blank=True, null=True)
	floor_level = models.TextField(blank=True, null=True)
	amenity_value = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	
	# Dates
	move_out = models.DateField(blank=True, null=True)
	move_out_date = models.DateField(blank=True, null=True)
	date_unit_available = models.DateField(blank=True, null=True)
	move_in_date = models.DateField(blank=True, null=True)
	scheduled_move_in = models.DateField(blank=True, null=True)
	lease_start = models.DateField(blank=True, null=True)
	lease_end = models.DateField(blank=True, null=True)
	
	# Metrics
	turn_time = models.IntegerField(blank=True, null=True)
	status = models.TextField(blank=True, null=True)
	not_ready_past_dates = models.TextField(blank=True, null=True)
	mr_day_variance = models.IntegerField(blank=True, null=True)
	leased_not_leased = models.TextField(blank=True, null=True)
	days_on_market = models.IntegerField(blank=True, null=True)
	days_until_vacant = models.IntegerField(blank=True, null=True)
	
	# Financial
	market_rent = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	lease_rent = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	effective_rent = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	lease_term = models.IntegerField(blank=True, null=True)
	month_price_12 = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True, db_column='12_month_price')
	best_term = models.IntegerField(blank=True, null=True)
	best_price = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	forecasted_trade_out = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	
	# Additional info
	move_out_reason = models.TextField(blank=True, null=True)
	resident_name = models.TextField(blank=True, null=True)
	ntv_flag = models.TextField(blank=True, null=True)
	exposure_8_weeks = models.FloatField(blank=True, null=True)
	unit_type = models.TextField(blank=True, null=True)
	leased_not_leased = models.TextField(blank=True, null=True, db_column='Leased/Not Leased')
	vacant_status = models.TextField(blank=True, null=True, db_column='Vacant Status')
	unit_available_flag = models.TextField(blank=True, null=True)
	days_vacant = models.IntegerField(blank=True, null=True)
	# mr_day_variance_2 = models.IntegerField(blank=True, null=True, db_column='mr_day_variance')
	unit_vacant_over_30 = models.TextField(blank=True, null=True)
	
	# Organizational hierarchy
	investor = models.TextField(blank=True, null=True)
	regional_vp = models.TextField(blank=True, null=True)
	regional_manager = models.TextField(blank=True, null=True)
	regional_area_manager = models.TextField(blank=True, null=True)
	regional_director = models.TextField(blank=True, null=True)
	community = models.TextField(blank=True, null=True)
	
	# Site identifier
	site_unit_id = models.TextField(blank=True, null=True)
	
	class Meta:
		managed = False
		db_table = '"web_ai"."total_unit_occupancy_drill_through"'


class DelinquencyDrillThrough(models.Model):
	"""Unmanaged materialized view for delinquency drill-through data.
	
	Maps to web_ai.delinquency_drill_through materialized view (28 columns).
	Data source: Contains detailed delinquency records with financial breakdowns.
	"""
	# Identification columns
	property_name = models.TextField(blank=True, null=True)
	unit_number_name = models.TextField(blank=True, null=True, db_column='Unit_Number/Name')
	unit_number = models.CharField(max_length=20, blank=True, null=True)
	
	# Categorization columns
	code_description = models.CharField(max_length=60, blank=True, null=True)
	delinquency_status = models.TextField(blank=True, null=True)
	is_employee_lease_status = models.TextField(blank=True, null=True)
	late_nsf_value = models.TextField(blank=True, null=True)
	is_under_eviction = models.TextField(blank=True, null=True)
	
	# Financial columns - primary delinquency amounts
	total_delinquent = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	total_prepaid = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	
	# Age bucket columns
	days_0_30 = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True, db_column='0-30 Days')
	days_30_60 = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True, db_column='30-60 Days')
	days_60_90 = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True, db_column='60-90 Days')
	days_90_plus = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True, db_column='90 plus days')
	
	# Additional financial details
	prorate_credits = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	deposits_held = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	outstanding_deposit = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	
	# Date columns
	notice_date = models.DateField(blank=True, null=True)
	fiscal_as_of_month_year = models.TextField(blank=True, null=True, db_column='Fiscal As Of month Year')
	fiscal_as_of = models.DateTimeField(blank=True, null=True)
	delinquent_as_of_date = models.DateTimeField(blank=True, null=True)
	
	# Organizational hierarchy
	community = models.TextField(blank=True, null=True)
	regional_vp = models.CharField(max_length=100, blank=True, null=True, db_column='Regional VP  | Sr. VP')
	regional_area_manager = models.CharField(max_length=100, blank=True, null=True)
	investor = models.CharField(max_length=255, blank=True, null=True)
	
	# Metadata columns
	row_num = models.BigIntegerField(primary_key=True, db_column='RowNum')
	total_delinquent_as_of_date = models.TextField(blank=True, null=True)
	total_prepaid_as_of_date = models.DecimalField(max_digits=19, decimal_places=2, blank=True, null=True)
	
	class Meta:
		managed = False
		db_table = '"web_ai"."delinquency_drill_through"'
