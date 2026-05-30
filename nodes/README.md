## Data Availability

The original node data file is not included in this repository because it exceeds GitHub's file size limit.

You can download the original node data from the following Google Drive link:

- Download nodes.jsonl

After downloading the file, place it in the following directory:

bash nodes/nodes.jsonl 

The expected project structure is:

bash . ├── nodes │   └── nodes.jsonl ├── ... 

If the nodes directory does not exist, create it manually:

bash mkdir -p nodes 

Then move the downloaded file into the directory:

bash mv nodes.jsonl nodes/nodes.jsonl 

Large generated or raw data files such as nodes/nodes.jsonl are excluded from Git tracking to keep the repository lightweight and compatible with GitHub's file size policy.