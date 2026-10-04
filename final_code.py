from dataclasses import fields
import os
import re
import yaml
import json
import requests
import certifi
import smtplib
import configparser
from jira import JIRA
from datetime import datetime
from pydantic import SecretStr
from email.mime.text import MIMEText
from pymongo import MongoClient, errors
from langchain_openai import ChatOpenAI
from email.mime.multipart import MIMEMultipart

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- Load Credentials ----------------
CREDS_FILE = os.path.join(BASE_DIR, 'creds.ini')
creds = configparser.ConfigParser()
creds.read(CREDS_FILE)
print("🛠️ Loading configuration...")

# ---------------- File Paths ----------------
yaml_path = os.path.join(BASE_DIR, "prompt.yaml")
iop_file_path = os.path.join(BASE_DIR, "IOP_Service_Ticket.json")
jira_template_path = os.path.join(BASE_DIR, "mandatory_fields.json")

# ---------------- Email Configuration ----------------
SENDER_EMAIL = creds["email"]["sender_email"]
RECEIVER_EMAILS = eval(creds["email"]["receiver_emails"])

# ---------------- MongoDB Connection ----------------
def mongo_connection():
    uri = "mongodb+srv://mganes157:HakunaMatata9940@ticketpilot.jfikitk.mongodb.net/?retryWrites=true&w=majority&appName=TicketPilot"
    try:
        client = MongoClient(uri, tls=True, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
        db = client["field_tickets"]
        collection = db["iop_service_tickets"]
        db.command("ping")
        print("✅ MongoDB connection successful.")
        return collection
    except errors.PyMongoError as e:
        print(f"⚠️ MongoDB connection failed")
        return None

# ---------------- SAT Token Retrieval ----------------

url = "https://sat-prod.codebig2.net/v2/oauth/token"
headers = {
    "Authorization": "No Auth",
    "Content-Type": "application/form-data",
    "Accept": "application/json",
    "x-client-id": "ticket-pilot",
    "x-client-secret": "e025f8dcc6b27e5c87e19eb7e1e28acd"
}

try: 
    response = requests.post(url, headers=headers, data="")
    llm = ChatOpenAI(
        model=creds["llm"]["model"],
        openai_api_base=creds["llm"]["openai_api_base"],
        api_key=SecretStr(response.json().get('access_token')),
        streaming=False,
    )
    print("✅ SAT token retrieval successful.")
except requests.RequestException as e:
    print(f"⚠️ Request failed: {e}")
    
# ---------------------- Global JIRA ----------------------
jira_options = {'server': creds["jira"]["jira_server"]}
try:
    jira = JIRA(
        options=jira_options,
        basic_auth=(creds["jira"]["jira_username"], creds["jira"]["jira_password"])
    )
    print("✅ JIRA connection successful.")
except Exception as e:
    print(f"⚠️ JIRA connection failed: {e}")

# ------------------ Load YAML -------------------
with open(yaml_path, "r") as ymlfile:
    prompts = yaml.safe_load(ymlfile)

# ------------------ Load IOP JSON ----------------
with open(iop_file_path, "r") as file:
    iop_data = json.load(file)

# ------- Load Template JSON ----------------
with open(jira_template_path, "r") as f:
    jira_template_data = json.load(f)

# ---------------- Upsert IOP Data ----------------
def upsert_iop_data():
    global iop_data
    collection = mongo_connection()
    for item in iop_data:
        collection.update_one(
            {"_id": item["_id"]},
            {"$set": item},
            upsert=True
        )
    print("✅ IOP data upserted successfully.")

# ---------------- LLM Prompt Construction ----------------
def llm_prompt_construction(doc):
    global llm, prompts  # Ensure llm and prompts are accessible within the function

    change_details = doc.get("change_details", {})
    incident_description = change_details.get("description", None)
    pre_activity_validation = change_details.get("pre_activity_validation", None)
    impacted_partners = change_details.get("impacted_partners", None)

    created_on = (
        datetime.strptime(change_details["created_on"], "%Y-%m-%d %I:%M:%S %p").strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        if change_details.get("created_on") 
        else datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    )

    # Fill template
    prompt = prompts["jira_creation_prompt"].format(
        incident_description=incident_description,
        pre_activity_validation=pre_activity_validation,
        impacted_partners=impacted_partners
    )

    telemetry_marker = pre_activity_validation.split(",")[-1].split(":")[-1].strip()
    opened_in_build = pre_activity_validation.split(",")[0].split(":")[-1].strip()

    try:
        response = llm.invoke([
                {"role": "system", "content": "You are a helpful assistant that triages ServiceNow incidents."},
                {"role": "user", "content": prompt}
            ])

        # ✅ Try JSON parsing
        try:
            llm_response_content = json.loads(response.content)
            # ✅ Extract fields
            summary = llm_response_content.get("summary", "")
            description = llm_response_content.get("description", "")
            suggested_fix = llm_response_content.get("suggested_fix", "")
            priority = llm_response_content.get("priority", "")
            return summary, description, suggested_fix, telemetry_marker, opened_in_build, created_on, priority

        except json.JSONDecodeError:
            print("⚠️ Could not parse JSON, raw LLM response:")
            print(response.content)
            return None
               
    except Exception as e:
        print(f"⚠️ LLM invocation failed: {e}")
        return None

# ---------------- IOP Status Check ----------------
def iop_status_check():
    global jira, iop_data, jira_template_data  # Ensure jira and collection are accessible within the function

    collection = mongo_connection()

    if collection is not None:
        final_data = list(collection.find({}))
    else:
        final_data = iop_data  # Use local JSON data

    for doc in final_data:
        if "jira_tickets" in doc and doc["jira_tickets"]:       # ✅ Skip if "jira_tickets" exists and is non-empty
            print(f"⏭️ Skipping {doc.get('_id')} (already has Jira tickets)")
            continue  

        # ---------------- LLM Prompt Response ----------------
        summary, description, suggested_fix, telemetry_marker, opened_in_build, created_on, priority = llm_prompt_construction(doc)

        # ---------------- Build Jira Payload ----------------
        payload = jira_template_data.copy()  # start from template

        payload["summary"] = summary
        payload["description"] = description
        payload["priority"] = {"name": priority} 
        payload["Telemarker"] = telemetry_marker
        payload["Opened in Build"] = opened_in_build
        payload["Defect Date / Time"] = created_on
        payload["Defect Recovery Steps"] = re.search(r"\*\*Recovery_steps:\*\*(.*?)(\n\*\*|$)", description, re.DOTALL).group(1).strip()

        fields = {
            "summary":  payload["summary"],
            "description": payload["description"],
            "priority": payload["priority"],
            "project": payload["project"],
            "issuetype": payload["issuetype"],
            "assignee": payload["assignee"],
            "reporter": payload["reporter"],
            "customfield_25857": payload["Blocker"],
            "customfield_25858": payload["Regression"],
            "customfield_26257": payload["Branch"],
            "customfield_30551": payload["Feature"],
            "customfield_20540": payload["Frequency"],
            "customfield_12240": payload["Impacted Products"],
            "customfield_10392": payload["Telemarker"],
            "customfield_39884": payload["Defect Recovery Steps"],
            "customfield_22979": payload["Feature Area"],
            "customfield_18148": payload["Device Type"],
            "customfield_15746": payload["Type of Issue"],
            "customfield_21742": payload["Opened in Build"],
            "customfield_26855": payload["Defect Date / Time"],
        }

        try:
            new_issue = jira.create_issue(fields=fields)
            print(f"✅ Created JIRA ticket: {new_issue.key}")
               
            jira.add_comment(new_issue, suggested_fix)
            print(f"📝 Added comment to {new_issue.key} with similar tickets.")

            jira_ticket_entry = {
                "key": new_issue.key,
                "url": f"{creds['jira']['jira_server']}/browse/{new_issue.key}"
            }

            if collection is not None:   # ✅ MongoDB available
                collection.update_one(
                    {"_id": doc["_id"]},
                    {"$push": {"jira_tickets": jira_ticket_entry}}
                )
                print(f"📝 Updated MongoDB doc {doc['_id']} with JIRA {new_issue.key}")
            else:            # ❌ MongoDB not available → update JSON file
                for item in iop_data:  # iop_data is your in-memory JSON list
                    if item["_id"] == doc["_id"]:
                        item.setdefault("jira_tickets", []).append(jira_ticket_entry)

                # Write back to file
                with open(iop_file_path, "w") as f:
                    json.dump(iop_data, f, indent=4)
                print(f"📝 Updated local JSON file with JIRA {new_issue.key}")

        except Exception as e:
            print(f"⚠️ Failed to create JIRA issue for {doc.get('_id')}: {e}")

# ----------------Jira Check for Mail Creation ----------------
def jira_check_for_mail_creation(ticket_key, incident_number):
    global jira  # Ensure jira is accessible within the function

    try:
        issue = jira.issue(ticket_key)
        if issue.fields.status.name.lower() == "closed":  # ✅ Only proceed if status is "Closed"
            created_date = issue.fields.created
            updated_date = issue.fields.updated

            # Convert to datetime
            created_dt = datetime.strptime(created_date, "%Y-%m-%dT%H:%M:%S.%f%z")
            updated_dt = datetime.strptime(updated_date, "%Y-%m-%dT%H:%M:%S.%f%z")

            # Calculate turnaround time
            turnaround = updated_dt - created_dt
            days = turnaround.days
            hours = turnaround.seconds // 3600
            turnaround_str = f"{days} days {hours} hours"

            jira_details = {
                "status": issue.fields.status.name,
                "comments": [c.body for c in jira.comments(issue)],
                "resolution": issue.fields.resolution.name if issue.fields.resolution else "Unresolved",
                "created_date": created_date,
                "last_updated": updated_date,
                "turnaround_time": turnaround_str
            }

            # ---------------- Fill template from YAML ----------------
            prompt_template = prompts["mail_creation_prompt"]
            prompt = prompt_template.format(
                incident_id=incident_number,
                project_id=ticket_key,
                status=jira_details["status"],
                resolution=jira_details["resolution"],
                comments_text="\n".join(jira_details["comments"]),
                turnaround_time=jira_details["turnaround_time"]
            )

            return prompt   # ✅ only return the prompt   

        else:
            print(f"⏭️ JIRA issue {ticket_key} is not in 'New' status (current: {issue.fields.status.name})")
            return None     

    except Exception as e:
        print(f"⚠️ Could not fetch JIRA issue {ticket_key}: {e}")

# ---------------- Email Sending ----------------
def send_email(subject, body):
    try:
        message = MIMEMultipart()
        message["From"] = SENDER_EMAIL
        message["To"] = ", ".join(RECEIVER_EMAILS)
        message["Subject"] = subject

        message.attach(MIMEText(body, "plain"))
        print("email content:", message.as_string())

        smtp_server = smtplib.SMTP("mailrelay.comcast.com")
        smtp_server.sendmail(SENDER_EMAIL, RECEIVER_EMAILS, message.as_string())
        smtp_server.quit()

        print(f"✅ Email sent successfully to {RECEIVER_EMAILS} via mailrelay.comcast.com!")
    except Exception as e:
        print(f"❌ Failed to send email. Error: {e}")

# ---------------- Mail Creation Check ----------------
def mail_creation_check():
    global llm  # Ensure llm is accessible within the function

    collection = mongo_connection()
    if collection is not None:
        final_data = list(collection.find({}))
    else:
        final_data = iop_data  # Use local JSON data

    for doc in final_data:
        if "jira_tickets" in doc and doc["jira_tickets"]:
            ticket_key = [ticket["key"] for ticket in doc["jira_tickets"]]
            incident_number = doc.get("number", "N/A")

            for key in ticket_key:
                mail_prompt = jira_check_for_mail_creation(key, incident_number)

                if mail_prompt is not None:

                    mail_summary = llm.invoke([
                        {"role": "system", "content": "You are a helpful assistant that summarizes JIRA issues."},
                        {"role": "user", "content": mail_prompt}
                    ])

                    # ✅ Try JSON parsing
                    try:
                        llm_response_content = json.loads(mail_summary.content)

                        # Fill template from YAML
                        template = prompts["mail_summary_template"]
                        body = template.format(
                            incident_id=llm_response_content["incident_id"],
                            tracking_ticket=llm_response_content["summary"]["tracking_ticket"],
                            incident_details=llm_response_content["summary"]["incident_details"],
                            root_cause=llm_response_content["summary"]["root_cause"],
                            fix_provided=llm_response_content["summary"]["fix_provided"],
                            turnaround_time=llm_response_content["summary"]["turnaround_time"],
                            deployment_details=llm_response_content["summary"]["deployment_details"]
                        )
                        subject = f"[Incident Update] {llm_response_content['incident_id']} Resolved"
                        print(f"✅ Prepared email for incident {llm_response_content['incident_id']}")
                        send_email(subject, body)
                        
                    except json.JSONDecodeError:
                        print("⚠️ Could not parse JSON, raw LLM response:")
                        print(mail_summary.content)
                
                else:
                    print(f"⏭️ No mail prompt generated for JIRA {key}")



# ---------------- Main Execution ----------------  
if __name__ == "__main__":

    upsert_iop_data()
    iop_status_check()
    mail_creation_check()