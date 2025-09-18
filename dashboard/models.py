from django.db import models



class OlympusLeaseTrendAnalysis(models.Model):
	snapshotdate = models.CharField(max_length=50)
	property_number = models.IntegerField()
	property_name = models.CharField(max_length=50)
	investor = models.CharField(max_length=50)
	regional_area_manager = models.CharField(max_length=50)
	regional_director = models.CharField(max_length=50)
	senior_regional = models.CharField(max_length=50)
	asst_manager = models.CharField(max_length=50)
	total_units = models.IntegerField()
	occupied_units = models.IntegerField()
	percentage_occupacy = models.FloatField()
	total_move_ins = models.IntegerField()
	total_move_outs = models.IntegerField()
	avg_amount_per_sqft = models.CharField(max_length=50)
	effective_rent = models.CharField(max_length=50)
	market_rent = models.CharField(max_length=50)
	visit = models.CharField(max_length=50)
	applications = models.CharField(max_length=50)
	cancelled = models.CharField(max_length=50)
	denied = models.CharField(max_length=50)
	vacant_pre_leased = models.CharField(max_length=50)
	occupied_pre_leased = models.CharField(max_length=50)
	NTV = models.CharField(max_length=50)
	make_ready = models.CharField(max_length=50)
	MTM = models.CharField(max_length=50)
	current_month_expiring = models.CharField(max_length=50)
	next_month_expiring = models.CharField(max_length=50)
	month_after_next_expiring = models.CharField(max_length=50)
	tbd_90_days = models.CharField(max_length=50)
	down = models.CharField(max_length=50)
	vacant_units_without_down_admin = models.CharField(max_length=50)

	class Meta:
		db_table = 'olympus_lease_trend_analysis'
