import boto3, logging, json, time, pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2          = boto3.client("ec2")
ssm          = boto3.client("ssm")
lambda_client = boto3.client("lambda")   # Slack 전송용

# ────────── RDS 연결 ────────── #
RDS_HOST = "mysql-multi-db.cxq0qomeyx1k.us-east-1.rds.amazonaws.com"
RDS_USER = "admin"
RDS_PASSWORD = "zaqxsw11!"
RDS_DB   = "hospital"

def get_db_conn():
    return pymysql.connect(
        host=RDS_HOST, user=RDS_USER, password=RDS_PASSWORD,
        database=RDS_DB, autocommit=True, connect_timeout=5
    )

# ────────── 매핑 테이블 ────────── #
ROUTE_TABLES = {
    "i-0b63747b3d1f52972": {"ward": "A", "route_table_id": "rtb-0d0465e7f748974dd", "cidr": "10.0.10.0/23"},
    "i-0fd919c3aa8dea3e9": {"ward": "A", "route_table_id": "rtb-0d0465e7f748974dd", "cidr": "10.0.10.0/23"},
    "i-045763e17c20e1b8a": {"ward": "B", "route_table_id": "rtb-0b3df0675665cb0bd", "cidr": "10.0.20.0/23"},
    "i-0a868727c52163cf1": {"ward": "B", "route_table_id": "rtb-0b3df0675665cb0bd", "cidr": "10.0.20.0/23"},
    "i-0cdf03f03f0353f3f": {"ward": "C", "route_table_id": "rtb-00e15e44dc2563340", "cidr": "10.0.30.0/23"},
    "i-056a592094da69fe6": {"ward": "C", "route_table_id": "rtb-00e15e44dc2563340", "cidr": "10.0.30.0/23"},
}
WARD_GATEWAYS = {"A": "10.0.10.1", "B": "10.0.20.1", "C": "10.0.30.1"}
WARD_SG      = {"A": "sg-01c5f25b8bf5a0883", "B": "sg-086f0317e74950315", "C": "sg-0b63679cde4d92c80"}
DEFAULT_SG   = "sg-01c5f25b8bf5a0883"

# ────────── DB 헬퍼 ────────── #
def update_status(ward, instance_id, status):
    try:
        with get_db_conn() as conn, conn.cursor() as c:
            c.execute(
                """INSERT INTO ward_instance_status (ward,instance_id,status)
                   VALUES (%s,%s,%s)
                   ON DUPLICATE KEY UPDATE status=VALUES(status),updated_at=NOW()""",
                (ward, instance_id, status)
            )
        logger.info(f"[DB] status {ward} {instance_id} → {status}")
    except Exception as e:
        logger.error(f"[DB] status upsert 실패: {e}")

def record_history(fault_ward, from_inst, to_ward, to_id,
                   rt_id, cidr, status="SUCCESS", msg="", trig="lambda"):
    try:
        with get_db_conn() as conn, conn.cursor() as c:
            c.execute(
                """INSERT INTO ward_route_history
                   (fault_ward,from_instance_id,to_ward,to_instance_id,
                    route_table_id,destination_cidr,status,message,triggered_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (fault_ward, from_inst, to_ward, to_id,
                 rt_id, cidr, status, msg, trig)
            )
        logger.info(f"[DB] history {from_inst} → {to_id}")
    except Exception as e:
        logger.error(f"[DB] history insert 실패: {e}")

# ────────── 장애 로그 복사 함수 ────────── #
def record_fault_log_from_history():
    try:
        with get_db_conn() as conn, conn.cursor() as c:
            insert_query = """
            INSERT INTO ward_route_fault_log (
                ward_id, from_instance_id, to_instance_id, from_eni_id, to_eni_id, event_type, result, timestamp
            )
            SELECT 
                fault_ward, 
                from_instance_id, 
                to_instance_id, 
                from_instance_id AS from_eni_id,
                to_instance_id AS to_eni_id,
                triggered_by AS event_type,
                status AS result,
                timestamp
            FROM ward_route_history
            WHERE status != 'SUCCESS'
              AND NOT EXISTS (
                  SELECT 1 FROM ward_route_fault_log 
                  WHERE ward_route_fault_log.timestamp = ward_route_history.timestamp
                    AND ward_route_fault_log.from_instance_id = ward_route_history.from_instance_id
                    AND ward_route_fault_log.to_instance_id = ward_route_history.to_instance_id
              )
            """
            c.execute(insert_query)
        logger.info("[DB] 장애 로그를 ward_route_fault_log 테이블에 기록 완료")
    except Exception as e:
        logger.error(f"[DB] 장애 로그 기록 실패: {e}")

# ────────── 보조 헬퍼 ────────── #
def wait_instance_ready(iid, timeout=420, poll=10):
    elapsed = 0
    while elapsed < timeout:
        st = ec2.describe_instance_status(InstanceIds=[iid], IncludeAllInstances=True)["InstanceStatuses"]
        ec2_ok = st and st[0]["SystemStatus"]["Status"] == "ok" and st[0]["InstanceStatus"]["Status"] == "ok"
        try:
            ssm_ok = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [iid]}]
            )["InstanceInformationList"][0]["PingStatus"] == "Online"
        except Exception:
            ssm_ok = False
        logger.info(f"[WAIT] {iid} EC2:{ec2_ok} SSM:{ssm_ok} ({elapsed}s)")
        if ec2_ok and ssm_ok:
            return True
        time.sleep(poll)
        elapsed += poll
    return False

def extract_instance_id_from_alarm(name):
    return {
        "WARD-A_i-a1_HealthCheck": "i-0b63747b3d1f52972",
        "WARD-A_i-a2_HealthCheck": "i-0fd919c3aa8dea3e9",
        "WARD-B_i-b1_HealthCheck": "i-045763e17c20e1b8a",
        "WARD-B_i-b2_HealthCheck": "i-0a868727c52163cf1",
        "WARD-C_i-c1_HealthCheck": "i-0cdf03f03f0353f3f",
        "WARD-C_i-c2_HealthCheck": "i-056a592094da69fe6",
    }.get(name)

# ────────── Lambda 핸들러 ────────── #
def lambda_handler(event, _):
    logger.info("=== EVENT ===\n" + json.dumps(event))
    instance_id, trig = None, None

    if event.get("Records") and event["Records"][0]["EventSource"] == "aws:sns":
        alarm = json.loads(event["Records"][0]["Sns"]["Message"])
        instance_id = extract_instance_id_from_alarm(alarm.get("AlarmName"))
        trig = "cloudwatch"

    elif event.get("source") == "aws.ec2" and event["detail"].get("eventName") == "StopInstances":
        instance_id = event["detail"]["requestParameters"]["instancesSet"]["items"][0]["instanceId"]
        trig = "cloudtrail"

    elif event.get("source") == "aws.ec2" and event["detail"].get("eventName") in ("DetachNetworkInterface", "DeleteNetworkInterface"):
        instance_id = event["detail"]["requestParameters"].get("instanceId")
        trig = "cloudwatch"

    if not instance_id or instance_id not in ROUTE_TABLES:
        return {"statusCode": 400, "body": "Unsupported instance"}

    return _process(instance_id, trig)

# ────────── 복구 로직 ────────── #
def _process(instance_id, trig):
    cfg  = ROUTE_TABLES[instance_id]
    ward = cfg["ward"]

    instance_desc = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    old_eni_id = instance_desc["NetworkInterfaces"][0]["NetworkInterfaceId"]
    subnet     = instance_desc["SubnetId"]

    if trig == "cloudtrail":
        update_status(ward, instance_id, "stopping")
        record_history(ward, instance_id, ward, instance_id, cfg["route_table_id"], cfg["cidr"],
                       status="DETECTED", msg="StopInstances", trig="cloudtrail")

        for _ in range(30):
            if ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]["State"]["Name"] == "stopped":
                break
            time.sleep(2)

        ec2.start_instances(InstanceIds=[instance_id])
        update_status(ward, instance_id, "starting")

    else:
        update_status(ward, instance_id, "network-fault")
        record_history(ward, old_eni_id, ward, instance_id, cfg["route_table_id"], cfg["cidr"],
                       status="NETWORK_RECOVERY", msg="ENI 장애 감지", trig="cloudwatch")

    eni_id = ec2.create_network_interface(
        SubnetId=subnet,
        Groups=[WARD_SG.get(ward, DEFAULT_SG)],
        Description="Auto-recovery ENI"
    )["NetworkInterface"]["NetworkInterfaceId"]

    for _ in range(30):
        state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]["State"]["Name"]
        if state in ("stopped", "running"):
            break
        time.sleep(2)
    else:
        logger.warning(f"⚠️ {instance_id} 상태가 'running'이나 'stopped'가 아니어서 ENI attach 생략")
        return {"statusCode": 500, "body": f"{instance_id} ENI attach 실패: 상태={state}"}

    ec2.attach_network_interface(NetworkInterfaceId=eni_id, InstanceId=instance_id, DeviceIndex=1)

    record_history(ward, old_eni_id, ward, eni_id,
                   cfg["route_table_id"], cfg["cidr"],
                   status="ENI_CREATED", msg="Failover ENI 생성 및 부착", trig=trig)

    if wait_instance_ready(instance_id):
        try:
            ec2.replace_route(RouteTableId=cfg["route_table_id"],
                              DestinationCidrBlock=cfg["cidr"],
                              NetworkInterfaceId=eni_id)
        except ec2.exceptions.ClientError:
            ec2.create_route(RouteTableId=cfg["route_table_id"],
                             DestinationCidrBlock=cfg["cidr"],
                             NetworkInterfaceId=eni_id)

        ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [
                    "IFACE=$(ip -o -4 route get 1 | awk '{print $5;exit}')",
                    f"sudo ip route replace default via {WARD_GATEWAYS[ward]} dev $IFACE"
                ]
            },
            Comment="Fail-over ENI route update",
            TimeoutSeconds=120
        )
        update_status(ward, instance_id, "running")
        record_history(ward, eni_id, ward, instance_id,
                       cfg["route_table_id"], cfg["cidr"],
                       status="SUCCESS", msg="라우팅 전환 및 SSM 네트워크 재설정 완료", trig=trig)
    else:
        update_status(ward, instance_id, "init-timeout")
        record_history(ward, eni_id, ward, instance_id,
                       cfg["route_table_id"], cfg["cidr"],
                       status="FAILURE", msg="EC2 or SSM timeout", trig=trig)

    lambda_client.invoke(
        FunctionName="Slack-SNS",
        InvocationType="Event",
        Payload=json.dumps({
            "type": "ROUTE_RECOVERY",
            "ward": ward,
            "instance_id": instance_id,
            "eni_id": eni_id,
            "status": "SUCCESS",
            "triggered_by": trig
        }).encode()
    )

    return {"statusCode": 200, "body": f"{instance_id} recovery done"}
