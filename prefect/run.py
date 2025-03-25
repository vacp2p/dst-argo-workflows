from prefect import flow, task
from prefect.server.schemas.schedules import IntervalSchedule
from datetime import timedelta
import base64
import requests
import json
import os

AUTHORIZED_USERS = ["zorlin", "AlbertoSoutullo", "michatinkers"]

@task
def find_valid_issue(repo_name: str, github_token: str):
    url = f"https://api.github.com/repos/{repo_name}/issues"
    headers = {"Authorization": f"token {github_token}"}
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return "NOT_VERIFIED", None

    issues = response.json()
    for issue in issues:
        if "simulation-done" in [l['name'] for l in issue.get('labels', [])]:
            continue
        if "needs-scheduling" not in [l['name'] for l in issue.get('labels', [])]:
            continue

        events_url = f"https://api.github.com/repos/{repo_name}/issues/{issue['number']}/events"
        events = requests.get(events_url, headers=headers).json()
        for e in reversed(events):
            if e['event'] == 'labeled' and e['label']['name'] == 'needs-scheduling':
                if e['actor']['login'].lower() in [u.lower() for u in AUTHORIZED_USERS]:
                    encoded = base64.b64encode(json.dumps(issue).encode()).decode()
                    return "VERIFIED", encoded
    return "NOT_VERIFIED", None

@task
def parse_and_generate_matrix(valid_issue_encoded: str):
    try:
        issue = json.loads(base64.b64decode(valid_issue_encoded).decode())
        body = issue['body']
        issue_number = issue['number']
    except:
        return []

    lines = body.split('\n')
    data = {}
    current = None
    for line in lines:
        if line.startswith("### "):
            current = line[4:].strip()
            data[current] = ""
        elif current:
            data[current] += line + "\n"

    # Utility function to handle "_No response_" and empty values
    def get_valid_value(key, default_value=""):
        value = data.get(key, default_value).strip()
        if not value or value == "_No response_":
            return default_value
        return value
    
    def parse_list(val, default="0"):
        if not val or val == "_No response_":
            val = default
        return [int(v.strip()) for v in val.split(",") if v.strip().isdigit()]
    
    def safe_int(val, default):
        if not val or val == "_No response_":
            return default
        try:
            return int(val.strip()) if val and val.strip() else default
        except (ValueError, TypeError):
            return default
    
    def safe_bool(val, default=False):
        if not val or val == "_No response_":
            return default
        return val.lower() == "yes"

    # Parse all the configuration parameters using our utility functions
    nodes = parse_list(get_valid_value("Number of nodes", "50"))
    durations = parse_list(get_valid_value("Duration", "5"))
    parallel_runs = parse_list(get_valid_value("Parallelism", "1"))
    parallel_limit = parallel_runs[0] if parallel_runs else 1
    
    bootstrap_nodes = safe_int(get_valid_value("Bootstrap nodes"), 3)
    
    # Handle pubsub topic with validation
    raw_pubsub_topic = get_valid_value("PubSub Topic")
    if not raw_pubsub_topic or not raw_pubsub_topic.startswith("/waku/2/rs"):
        pubsub_topic = "/waku/2/rs/2/0"  # Default topic that is properly formatted
        print(f"Using default pubsub topic '{pubsub_topic}' because the provided value was invalid or missing")
    else:
        pubsub_topic = raw_pubsub_topic
        print(f"Using provided pubsub topic: {pubsub_topic}")
    
    publisher_enabled = safe_bool(get_valid_value("Enable Publisher"))
    publisher_message_size = safe_int(get_valid_value("Publisher Message Size"), 1)
    publisher_delay = safe_int(get_valid_value("Publisher Delay"), 10)
    publisher_message_count = safe_int(get_valid_value("Publisher Message Count"), 1000)
    
    artificial_latency = safe_bool(get_valid_value("Enable Artificial Latency"))
    latency_ms = safe_int(get_valid_value("Artificial Latency (ms)"), 50)
    
    nodes_command = get_valid_value("Nodes Command")
    bootstrap_command = get_valid_value("Bootstrap Command")

    image = get_valid_value("Docker image", "statusteam/nim-waku:latest")

    matrix = []
    i = 0
    for n in nodes:
        for d in durations:
            matrix.append({
                "index": i,
                "issue_number": issue_number,
                "nodecount": n,
                "duration": d,
                "bootstrap_nodes": bootstrap_nodes,
                "docker_image": image,
                "pubsub_topic": pubsub_topic,
                "publisher_enabled": publisher_enabled,
                "publisher_message_size": publisher_message_size,
                "publisher_delay": publisher_delay,
                "publisher_message_count": publisher_message_count,
                "artificial_latency": artificial_latency,
                "latency_ms": latency_ms,
                "nodes_command": nodes_command,
                "bootstrap_command": bootstrap_command,
                "parallel_limit": parallel_limit
            })
            i += 1
    return matrix

@task
def deploy_config(config: dict):
    print(f"Deploying config: nodes={config['nodecount']}, duration={config['duration']}, image={config['docker_image']}")
    
    import subprocess
    import yaml
    import time
    import os
    import re
    from datetime import datetime, timedelta

    # Helper function to process args properly
    def process_args(args_str):
        if not args_str:
            return []
        
        # Preserve template expressions by temporarily replacing them
        placeholders = {}
        pattern = r'({{[^}]+}})'
        
        def replace_templates(match):
            placeholder = f"TEMPLATE_PLACEHOLDER_{len(placeholders)}"
            placeholders[placeholder] = match.group(0)
            return placeholder
        
        # Replace template expressions with placeholders
        processed_str = re.sub(pattern, replace_templates, args_str)
        
        # Split on whitespace
        parts = processed_str.split()
        
        # Restore template expressions
        for i, part in enumerate(parts):
            for placeholder, template in placeholders.items():
                if placeholder in part:
                    parts[i] = part.replace(placeholder, template)
                    
        return parts
    
    # Extract values
    index = config.get("index", "unknown")
    nodecount = config.get("nodecount", 50)
    duration = config.get("duration", 5)
    bootstrap_nodes = config.get("bootstrap_nodes", 3)
    docker_image = config.get("docker_image", "statusteam/nim-waku:latest")
    pubsub_topic = config.get("pubsub_topic", "/waku/2/rs/2/0")
    publisher_enabled = config.get("publisher_enabled", False)
    publisher_message_size = config.get("publisher_message_size", 1)
    publisher_delay = config.get("publisher_delay", 10)
    publisher_message_count = config.get("publisher_message_count", 1000)
    artificial_latency = config.get("artificial_latency", False)
    latency_ms = config.get("latency_ms", 50)
    nodes_command = config.get("nodes_command", [])
    bootstrap_command = config.get("bootstrap_command", [])
    
    print(f"Pubsub topic: {pubsub_topic}")
    
    # Generate descriptive release name
    release_name = f"waku-{nodecount}x-{duration}m"
    
    print(f"Deploying configuration: {release_name} (nodes={nodecount}, duration={duration}m)")
    
    # Generate values.yaml
    values = {
        'global': {
            'pubSubTopic': pubsub_topic
        },
        'replicaCount': {
            'bootstrap': bootstrap_nodes,
            'nodes': nodecount
        },
        'image': {
            'repository': docker_image.split(':')[0] if ':' in docker_image else docker_image,
            'tag': docker_image.split(':')[1] if ':' in docker_image else 'latest',
            'pullPolicy': 'IfNotPresent'
        },
        'bootstrap': {
            'command': [bootstrap_command] if bootstrap_command and isinstance(bootstrap_command, str) else bootstrap_command or [],
            'resources': {
                'requests': {
                    'memory': "64Mi",
                    'cpu': "50m"
                },
                'limits': {
                    'memory': "768Mi",
                    'cpu': "400m"
                }
            }
        },
        'nodes': {
            'command': [nodes_command] if nodes_command and isinstance(nodes_command, str) else nodes_command or [],
            'resources': {
                'requests': {
                    'memory': "64Mi",
                    'cpu': "150m"
                },
                'limits': {
                    'memory': "600Mi",
                    'cpu': "500m"
                }
            }
        },
        'publisher': {
            'enabled': publisher_enabled,
            'image': {
                'repository': 'zorlin/publisher',
                'tag': 'v0.5.0'
            },
            'messageSize': publisher_message_size,
            'delaySeconds': publisher_delay,
            'messageCount': publisher_message_count,
            'startDelay': {
                'enabled': False,
                'minutes': 5
            },
            'waitForStatefulSet': {
                'enabled': True,
                'stabilityMinutes': 1
            }
        },
        'artificialLatency': {
            'enabled': artificial_latency,
            'latencyMs': latency_ms
        }
    }
    
    # Write values.yaml to a temporary file
    values_file = f"/tmp/values-{release_name}.yaml"
    with open(values_file, 'w') as f:
        yaml.dump(values, f)
    
    # Check if helm is installed, install if not
    try:
        subprocess.run(["helm", "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        print("Helm is already installed")
    except (subprocess.SubprocessError, FileNotFoundError):
        print("Installing Helm...")
        subprocess.run("curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3", shell=True)
        subprocess.run("chmod 700 get_helm.sh", shell=True)
        subprocess.run("./get_helm.sh", shell=True)
    
    # Deploy with Helm
    namespace = "zerotesting"
    chart_url = "https://github.com/vacp2p/dst-argo-workflows/raw/refs/heads/main/charts/waku-0.4.3.tgz"
    
    # Create namespace if it doesn't exist
    try:
        subprocess.run(["kubectl", "create", "namespace", namespace], 
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"Created namespace {namespace}")
    except subprocess.SubprocessError as e:
        print(f"Note: {namespace} namespace might already exist: {e}")
        
    helm_cmd = [
        "helm", "upgrade", "--install", release_name,
        chart_url,
        "-f", values_file,
        "--namespace", namespace
    ]
    
    # Record the start time of the simulation
    start_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"Starting simulation at: {start_time}")
    
    print(f"Running Helm command: {' '.join(helm_cmd)}")
    try:
        deploy_result = subprocess.run(helm_cmd, capture_output=True, text=True, check=True)
        print(f"Deployment successful:")
        print(f"Helm output: {deploy_result.stdout}")
    except subprocess.CalledProcessError as e:
        print(f"Error deploying: {e.stderr}")
        print(f"Helm output: {e.stdout}")
        raise
    
    # Wait for specified duration
    duration_seconds = duration * 60
    print(f"Waiting for {duration} minutes ({duration_seconds} seconds)...")
    
    # Wait in smaller chunks with progress updates
    chunk_size = 60  # Report progress every minute
    chunks = duration_seconds // chunk_size
    remainder = duration_seconds % chunk_size
    
    for i in range(chunks):
        time.sleep(chunk_size)
        print(f"Progress: {(i+1)*chunk_size}/{duration_seconds} seconds elapsed")
    
    if remainder > 0:
        time.sleep(remainder)
        print(f"Progress: {duration_seconds}/{duration_seconds} seconds elapsed")
    
    # Record the end time of the simulation
    end_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"Finished simulation at: {end_time}")
    
    # Clean up
    print("Cleaning up deployment...")
    cleanup_cmd = ["helm", "uninstall", release_name, "--namespace", namespace]
    try:
        cleanup_result = subprocess.run(cleanup_cmd, capture_output=True, text=True, check=True)
        print(f"Successfully cleaned up deployment {release_name}")
    except subprocess.CalledProcessError as e:
        print(f"Warning: Error during cleanup: {e.stderr}")
        print(f"Helm output: {e.stdout}")
    
    # Generate analysis script
    print("Generating analysis script...")
    analysis_dir = "analysis"
    os.makedirs(analysis_dir, exist_ok=True)
    
    analysis_script = f"""# Python Imports

# Project Imports
import src.logger.logger
from src.mesh_analysis.waku_message_log_analyzer import WakuMessageLogAnalyzer


if __name__ == '__main__':
    # Timestamp of the simulation   
    timestamp = "[{start_time}, {end_time}]"
    stateful_sets = ["bootstrap", "nodes"]
    # Example of data analysis from cluster
    log_analyzer = WakuMessageLogAnalyzer(stateful_sets, timestamp, dump_analysis_dir='local_data/{release_name}/')
    # Example of data analysis from local
    # log_analyzer = WakuMessageLogAnalyzer(local_folder_to_analyze='lpt_duptest_debug', dump_analysis_dir='lpt_duptest_debug/notion')

    log_analyzer.analyze_message_logs(True)
    log_analyzer.check_store_messages()
    log_analyzer.analyze_message_timestamps(time_difference_threshold=2)
"""
    
    analysis_file = f"{analysis_dir}/analyse_{release_name}.py"
    with open(analysis_file, "w") as f:
        f.write(analysis_script)
    
    print(f"Analysis script generated at {analysis_file}")
    
    # Clone and run analysis
    print("Cloning 10ksim repository...")
    try:
        # Check if already cloned
        if not os.path.exists("10ksim"):
            clone_cmd = ["git", "clone", "https://github.com/vacp2p/10ksim.git"]
            subprocess.run(clone_cmd, check=True)
        
        # Copy analysis script to the repository
        cp_cmd = ["cp", analysis_file, "10ksim/"]
        subprocess.run(cp_cmd, check=True)
        
        # Run the analysis
        print(f"Running analysis script...")
        analysis_run_cmd = ["python3", f"10ksim/analyse_{release_name}.py"]
        try:
            analysis_result = subprocess.run(analysis_run_cmd, capture_output=True, text=True, check=True)
            print(f"Analysis complete. Output:")
            print(analysis_result.stdout)
        except subprocess.CalledProcessError as e:
            print(f"Warning: Error during analysis: {e.stderr}")
            print(f"Analysis may require manual execution.")
    except Exception as e:
        print(f"Error during repository cloning or analysis: {e}")
        print("You may need to manually clone the repository and run the analysis script.")
    
    return f"Completed simulation for {nodecount} nodes running for {duration} minutes"

@flow
def waku_cron_job(repo_name: str, github_token: str):
    result, valid_issue = find_valid_issue(repo_name, github_token)
    if result == "VERIFIED" and valid_issue:
        matrix = parse_and_generate_matrix(valid_issue)
        
        # Get the parallelism value from the first config (should be the same for all)
        # Default to 1 if matrix is empty
        parallel_limit = 1
        if matrix:
            parallel_limit = matrix[0].get("parallel_limit", 1)
        
        print(f"Running with parallelism limit of {parallel_limit}")
        
        # Create a list to store all the futures
        active_futures = []
        
        for config in matrix:
            # If we've reached the parallelism limit, wait for one task to complete
            while len(active_futures) >= parallel_limit:
                # Wait for the first future to complete and remove it
                completed = active_futures.pop(0).wait()
            
            # Submit the next task
            active_futures.append(deploy_config.submit(config))
        
        # Wait for any remaining futures
        for future in active_futures:
            future.wait()

# Local debug run
if __name__ == "__main__":
    waku_cron_job(repo_name="vacp2p/vaclab", github_token="")
