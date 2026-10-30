# music-scripts
collection of scripts that help with downloading, retagging, renaming, sorting, and converting from flac to aac

There are 4 scripts, 
  fix_artist_tags.py
  flac_to_aac.py
  lidarr_add_artists.py
  tagging.py

the requirements.txt included with the scripts has all the requirements for all of the scripts



# How to use each script
fix_artist_tags.py
  it requires mutagen>=1.47
  it runs based on the directory the terminal is cd'd in to, so make sure to cd to where your music is
here's the command "python3 /path/to/fix_artist_tags.py"

flac_to_aac.py
  this one requires av>=12.0 and mutagen>=1.47
  the run directories are hard coded in the python file; SRC_ROOT and DST_ROOT; src is the source location and dst is where the converted music is saved
  it checks what's already been converted and will skip them 
  since the directories are hard coded, all that's needed is to run the script
  "python3 flac_to_aac.py"

lidarr_add_artists.py
  nothing extra is needed to download, but you do need to export lidarr url and key
  export LIDARR_URL=http://localhost:8686
  export LIDARR_API_KEY=your_api_key
  it pulls from a csv file. column A being labeled "artist" and the artists names under it. it searches lidarr based on this, so make sure it's accurate.
  there's a 5 minute delay between searching for artists so it doesn't overload the downloader, here's the command to run
  "python3 lidarr_add_artists.py artists.csv"

tagging.py
  this one requires mutagen>=1.47, pyacoustid>=1.3, musicbrainzngs>=0.7.1, requests>=2.31
  make sure to export your acoustid it's free on their website
  "export ACOUSTID_API_KEY=xxxxxxxx"
  "python3 tagging.py /music/unsorted /music/sorted --contact you@example.com"



  all of this has been made with claude, though it's all been tested by me extensively
