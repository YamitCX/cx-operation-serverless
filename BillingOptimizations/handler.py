import json, sys, os
import subprocess
import threading
import boto3
from datetime import datetime, date
from dateutil.relativedelta import *
from decimal import Decimal

subprocess.call('pip3 install pandas -t /tmp/ --no-cache-dir'.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.call('pip3 install elasticsearch -t /tmp/ --no-cache-dir'.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.call('pip install python-dotenv -t /tmp/ --no-cache-dir'.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


sys.path.insert(1, '/tmp/')

from ec2 import EC2
from aws_service import AwsService
from db_service import DbService
from performance_counters import PerformanceCounters
from dotenv import load_dotenv

def collect_ec2_utilization(ec2, metric_list, account_number, start_date, end_date):

    aws_service = AwsService()    
    db_service = DbService()

    frames = []   
                
    for metric_name in metric_list:
        statistics = 'Average'
        namespace = 'AWS/EC2'
        instance_id = ec2.instance_id
        period = 3600
        start_time = start_date
        end_time = end_date
                                            
        df = aws_service.get_aws_metric_statistics(ec2, metric_name, period, start_time, end_time, namespace, statistics)           
        
        if not df.empty:
            frames.append(df)           
        
    # merge the different dataframes (cpu_utilization, network_in...) into one dataframe based on start_time        
    if not frames == []:
        df_merged = db_service.merge_ec2_metrics_on_start_time(frames)
        df_merged['account_number'] = account_number   
                
        #convert the merged dataframe to class members to ease insert to Elasticsearch
        ec2.performance_counters_list = db_service.create_performance_counters_list(df_merged, metric_list)

        #insert the data into proper elastic index
        response =  db_service.ec2_bulk_insert_elastic(ec2)   
      

        

    
def collect_ec2_all(account_number, start_date, end_date):
    try:
        ec2_metric_list = ['CPUUtilization', 'NetworkOut', 'NetworkIn','DiskWriteBytes','DiskReadBytes','NetworkPacketsOut','NetworkPacketsIn','DiskWriteOps','DiskReadOps']            
        ec2_instances = []
        chunk_size = 10     
        
        aws_service = AwsService()    

        ec2_list = aws_service.get_aws_describe_instances()

        threads = []

        for i in range(0,len(ec2_list), chunk_size):
            chunk = ec2_list[i:i+chunk_size]
            for ec2 in chunk:
                x = threading.Thread(target=collect_ec2_utilization, args=(ec2, ec2_metric_list, account_number, start_date, end_date,))
                threads.append(x)
                x.start()
            for index, thread in enumerate(threads):                
                thread.join()                

            threads = []    
        
        for ec2 in ec2_list:
            print(f"instance_id: {ec2.instance_id} , owner_id: {ec2.instance_owner_id}, launch_time: {ec2.launch_time}")                
                
    except Exception as e:
        print(e)

def add_forcase_to_account_list(account_list):

    aws_service = AwsService() 

    for account in account_list:
        #start = account.start

        account_end = datetime.strptime(account.end, '%Y-%m-%d')
        today_datetime = datetime.combine(date.today(), datetime.min.time())
        
        if account_end != today_datetime:
            print("Cannot calculate forecast on historic data")
            continue
        
        today = date.today()

        start = today.strftime('%Y-%m-%d')

        start_datetime = datetime.strptime(start, '%Y-%m-%d')
        end_datetime = start_datetime.replace(day=1) + relativedelta(months=+1)
        end = end_datetime.strftime('%Y-%m-%d')

        # end is first day of next month
        #end = start.replace(day=1) + relativedelta(months=+1)
        response = aws_service.get_aws_cost_forecast(account.account_number,start, end, "MONTHLY", "AMORTIZED_COST", account.keys)

        if response != "":

            account.forecast_mean_value = round(Decimal(response['ForecastResultsByTime'][0]['MeanValue']),2)
            account.forecast_prediction_interval_lowerbound = round(Decimal(response['ForecastResultsByTime'][0]['PredictionIntervalLowerBound']),2)
            account.forecast_prediction_interval_upperbound = round(Decimal(response['ForecastResultsByTime'][0]['PredictionIntervalUpperBound']),2)

    return account_list

def collect_accounts_cost(account_number, start_date, end_date):

    aws_service = AwsService() 
    db_service = DbService()

    # in ordder to manipulate dates (compare, add ...), we must convert to datetime
    accounts_visibility_last_update_datetime = datetime.strptime(start_date, '%Y-%m-%d') 

    print(f"start = {start_date}, end = {end_date}")

    granularity = 'DAILY'
    metrics = 'AMORTIZED_COST'
    groupby = 'SERVICE'

    # get cost per account on the last month
    response = aws_service.get_aws_cost_and_usage(account_number, start_date, end_date, granularity, metrics, groupby)

    #create objects to hold the accounts cost data
    account_list = db_service.create_account(account_number, response)

    account_list_with_forecast = add_forcase_to_account_list(account_list)

    db_service.print_account_list(account_list)

    #insert accounts to elastic
    db_service.account_bulk_insert_elastic(account_list)    

    
def calcBillingOptimizations(event, context):

    client = boto3.client('sts')
    response = client.get_caller_identity()
    account_number = str(response['Account'])

    start_date = os.environ.get('LAMBDA_LAST_UPDATE')
    end_date = date.today().strftime('%Y-%m-%d')

    print (f"start_date = {start_date}")
    
    if start_date < end_date:                
        collect_accounts_cost(account_number, start_date, end_date)
        collect_ec2_all(account_number, start_date, end_date)    
    else:
        print(f"start date {start_date} and end date {end_date} are equal. exit...")

    client = boto3.client('lambda')

    response = client.update_function_configuration(
    FunctionName='billingoptimizations-prod-calcBillingOptimizations',
	Environment={
        'Variables': {
            'LAMBDA_LAST_UPDATE': end_date,            
            'ELASTIC_CONNECTIONSTRING': "https://elastic:kJ12iC0bfTVXo3qhpJqRLs87@c11f5bc9787c4c268d3b960ad866adc2.eu-central-1.aws.cloud.es.io:9243"
        }
    },
    )
        
    body = {'message':'Go Serverless v1.0! Your function executed successfully!',  'input':event}
    response = {'statusCode':200, 'body':json.dumps(body)}
    return response