import asyncio
import logging
import subprocess
import uuid

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)

# Store running tasks and logs
tasks = {}
logs = {}
import time
when_made = {}

import glob, os



async def raw_to_bam(accession: str, task_id: str):

    def do_log(s):
        logs[task_id].append(s)
        logging.info(s)

    do_log(f"Processing accession {accession}")

    # Download FASTQ files
    #cmd_fastq_dump = f'fasterq-dump --split-files {accession}'
    # using Aspera from ENA
    cmd_fastq_dump = f'fastq-dl -a {accession}'
    proc = await asyncio.create_subprocess_shell(cmd_fastq_dump)
    await proc.wait()
    
    # check what files were downloaded
    files = glob.glob(f"{accession}*.fastq.gz")
    do_log(f"Downloaded {len(files)} files: for {accession}")
    do_log(f"Files: {files}")
    type = None
    for file in files:
        if "1.fastq.gz" in file:
            type = "paired"
            break
        else:
            type = "single"
    max_reads = 100000
    # get number of reads in file
    if type == "paired":
        cmd_count_reads = f'zcat {accession}_1.fastq.gz | wc -l'
    else:
        cmd_count_reads = f'zcat {accession}.fastq.gz | wc -l'
    proc = await asyncio.create_subprocess_shell(cmd_count_reads, stdout=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    nreads = int(stdout.decode('utf-8').strip())
    if nreads > max_reads:
        do_log(f"Downsampling {accession} from {nreads} to {max_reads} reads")
        # downsample with seqtk
        if type == "paired":
            downsample_fraction = max_reads / nreads
            cmd_downsample = f'seqtk sample -s100 {accession}_1.fastq.gz {downsample_fraction} > {accession}_1.fastq.gz.tmp && mv {accession}_1.fastq.gz.tmp {accession}_1.fastq.gz'
            proc = await asyncio.create_subprocess_shell(cmd_downsample)
            await proc.wait()
            cmd_downsample = f'seqtk sample -s100 {accession}_2.fastq.gz {downsample_fraction} > {accession}_2.fastq.gz.tmp && mv {accession}_2.fastq.gz.tmp {accession}_2.fastq.gz'
            proc = await asyncio.create_subprocess_shell(cmd_downsample)
            await proc.wait()
        else:
            downsample_fraction = max_reads / nreads
            cmd_downsample = f'seqtk sample -s100 {accession}.fastq.gz {downsample_fraction} > {accession}.fastq.gz.tmp && mv {accession}.fastq.gz.tmp {accession}.fastq.gz'
            proc = await asyncio.create_subprocess_shell(cmd_downsample)
            await proc.wait()




    do_log(f"Type: {type}")

    # find how many cores are available
    cmd_nproc = f'nproc'
    proc = await asyncio.create_subprocess_shell(cmd_nproc, stdout=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    nproc = int(stdout.decode('utf-8').strip())
    do_log(f"Found {nproc} cores")

    


    if type == "paired":
        cmd_minimap2 = f'minimap2 -a ./ref.fa {accession}_1.fastq.gz {accession}_2.fastq.gz -t {nproc} | python count_lines.py {task_id}.lines |  samtools view -bS - > {accession}.bam'
    else:   
        cmd_minimap2 = f'minimap2 -a ./ref.fa {accession}.fastq.gz | python count_lines.py {task_id}.lines | samtools view -bS -t {nproc} - > {accession}.bam'
    
    proc = await asyncio.create_subprocess_shell(cmd_minimap2)
    await proc.wait()
    do_log(f"Aligned {accession} to reference genome")

    cmd_sort_bam = f'samtools sort {accession}.bam > {accession}.sorted.bam'
    proc = await asyncio.create_subprocess_shell(cmd_sort_bam)
    await proc.wait()
    do_log(f"Sorted BAM file for {accession}")

    cmd_index_bam = f'samtools index {accession}.sorted.bam'
    proc = await asyncio.create_subprocess_shell(cmd_index_bam)
    await proc.wait()
    do_log(f"Indexed BAM file for {accession}")

    # mv to /var/www/html
    cmd_mv = f'mv {accession}.sorted.bam* /var/www/html'
    proc = await asyncio.create_subprocess_shell(cmd_mv)
    await proc.wait()
    when_made[accession] = time.time()
    # we delete the fastq files now
    cmd_rm = f'rm {accession}*.fastq.gz'
    proc = await asyncio.create_subprocess_shell(cmd_rm)
    await proc.wait()

    # we delete any bam files older than 1 hour
    for file, when in when_made.items():
        if time.time() - when > 3600:
            cmd_rm = f'rm /var/www/html/{file}.sorted.bam*'
            proc = await asyncio.create_subprocess_shell(cmd_rm)
            await proc.wait()
            when_made.pop(file)

   
    do_log(f"Moved BAM file for {accession} to /var/www/html")

    do_log(f"Finished processing {accession}")

async def start_task(accession: str):
    task_id = str(uuid.uuid4())
    logs[task_id] = []
    task = asyncio.create_task(raw_to_bam(accession, task_id))
    tasks[task_id] = task
    return task_id

@app.post("/align/{accession}")
async def align(accession: str):
    logging.info(f"Aligning {accession} to reference genome")
    task_id = await start_task(accession)
    return {"task_id": task_id}

@app.get("/poll/{task_id}")
async def poll(task_id: str):
    if task_id not in tasks:
        return {"error": "Invalid task ID"}

    if not tasks[task_id].done():
        # read task_id.lines to get current line count
        try:
            with open(f"{task_id}.lines", "r") as f:
                lines = int(f.read())
        except:
            lines = 0
        
        return {"status": "processing", "log": logs[task_id], "lines": lines}

    if task_id in logs:
        return {"status": "complete", "log": logs[task_id]}
    else:
        return {"status": "complete"}
    


@app.get("/test")
async def test():
    return "hello world"

@app.get("/{path:path}")
async def return_from_root(path: str):
    logging.info(f"Returning file {path}")
    # Return from js/build/ directory
    def generate(path: str):
        if path == "":
            path = "index.html"
        with open(f"js/build/{path}", "rb") as file:
            yield from file
    
    return StreamingResponse(generate(path=path))
    