#!/bin/bash
# dyno number avail in $DYNO as per http://stackoverflow.com/questions/16372425/can-you-programmatically-access-current-heroku-dyno-id-name/16381078#16381078

for (( i=1; i<=$GREEN_SCRAPE_WORKERS_PER_DYNO; i++ ))
do
  COMMAND="python queue_green_oa_scrape.py --run --scrape-publisher --chunk=$GREEN_SCRAPE_CHUNK_SIZE"
  echo $COMMAND
  $COMMAND &
done
trap "kill 0" INT TERM EXIT
wait
