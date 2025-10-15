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
	exposure = models.IntegerField(blank=True, null=True)
	delinquency = models.IntegerField(blank=True, null=True)
	average_turn_time = models.IntegerField(blank=True, null=True)
	service_request = models.IntegerField(blank=True, null=True)
	renewel_conversion = models.IntegerField(blank=True, null=True)
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
