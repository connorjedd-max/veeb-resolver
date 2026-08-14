==> ///////////////////////////////////////////////////////////
INFO:     Shutting down
INFO:     Waiting for application shutdown.
INFO:     Application shutdown complete.
INFO:     Finished server process [1]
INFO:     10.29.140.2:0 - "POST /prefetch/UNvcr3rkXm8?intent=1 HTTP/1.1" 202 Accepted
direct innertube fast path missed {"videoId": "UNvcr3rkXm8", "purpose": "prefetch", "elapsedSeconds": 0.253, "error": "direct fast path failed: UNPLAYABLE | The page needs to be reloaded. || ERROR | This video is unavailable || web_safari: UNPLAYABLE | Video unavailable"}
in-process yt-dlp slot acquired {"videoId": "UNvcr3rkXm8", "purpose": "prefetch-prefetch", "pool": "prefetch", "engine": "prefetch-1", "queueWaitSeconds": 0.0, "availableAfterAcquire": 1}
cold resolve phase {"videoId": "UNvcr3rkXm8", "purpose": "prefetch-prefetch", "engine": "prefetch-1", "phase": "webpage", "elapsedSeconds": 0.091}
INFO:     10.26.104.132:0 - "GET /health HTTP/1.1" 200 OK
INFO:     10.29.140.2:0 - "POST /prefetch/DJdGFZOjuR4 HTTP/1.1" 202 Accepted
cold resolve phase {"videoId": "UNvcr3rkXm8", "purpose": "prefetch-prefetch", "engine": "prefetch-1", "phase": "player_api", "elapsedSeconds": 1.904}
INFO:     10.29.140.2:0 - "POST /prefetch/EcA12LVoUOA HTTP/1.1" 202 Accepted
cold resolve phase {"videoId": "UNvcr3rkXm8", "purpose": "prefetch-prefetch", "engine": "prefetch-1", "phase": "player_js", "elapsedSeconds": 2.296}
direct innertube fast path missed {"videoId": "DJdGFZOjuR4", "purpose": "prefetch", "elapsedSeconds": 0.308, "error": "direct fast path failed: UNPLAYABLE | The page needs to be reloaded. || ERROR | This video is unavailable || web_safari: UNPLAYABLE | Video unavailable"}
in-process yt-dlp slot acquired {"videoId": "DJdGFZOjuR4", "purpose": "prefetch-prefetch", "pool": "prefetch", "engine": "prefetch-2", "queueWaitSeconds": 0.0, "availableAfterAcquire": 0}
cold resolve phase {"videoId": "DJdGFZOjuR4", "purpose": "prefetch-prefetch", "engine": "prefetch-2", "phase": "webpage", "elapsedSeconds": 0.091}direct innertube fast path missed {"videoId": "EcA12LVoUOA", "purpose": "prefetch", "elapsedSeconds": 0.351, "error": "direct fast path failed: UNPLAYABLE | The page needs to be reloaded. || ERROR | This video is unavailable || web_safari: UNPLAYABLE | Video unavailable"}
cold resolve phase {"videoId": "UNvcr3rkXm8", "purpose": "prefetch-prefetch", "engine": "prefetch-1", "phase": "js_challenge", "elapsedSeconds": 2.691}
cold resolve phase {"videoId": "DJdGFZOjuR4", "purpose": "prefetch-prefetch", "engine": "prefetch-2", "phase": "player_api", "elapsedSeconds": 2.795}
cold resolve phase {"videoId": "DJdGFZOjuR4", "purpose": "prefetch-prefetch", "engine": "prefetch-2", "phase": "player_js", "elapsedSeconds": 3.49}
cold resolve phase {"videoId": "DJdGFZOjuR4", "purpose": "prefetch-prefetch", "engine": "prefetch-2", "phase": "js_challenge", "elapsedSeconds": 3.892}
INFO:     10.27.152.131:0 - "POST /prefetch/iaREBWprmvg HTTP/1.1" 202 Accepted
INFO:     10.27.152.131:0 - "POST /prefetch/EUy6Z6xxpAU HTTP/1.1" 202 Accepted
INFO:     10.29.140.2:0 - "POST /prefetch/SFfFs8HgwwI HTTP/1.1" 202 Accepted
INFO:     10.29.140.2:0 - "POST /prefetch/iaREBWprmvg?intent=1 HTTP/1.1" 202 Accepted
foreground bypassing speculative resolve {"videoId": "iaREBWprmvg"}
in-process yt-dlp slot acquired {"videoId": "iaREBWprmvg", "purpose": "live-auth", "pool": "fg-auth", "engine": "fg-auth-1", "queueWaitSeconds": 0.0, "availableAfterAcquire": 1}
in-process yt-dlp slot acquired {"videoId": "iaREBWprmvg", "purpose": "live-pot", "pool": "fg-pot", "engine": "fg-pot-1", "queueWaitSeconds": 0.0, "availableAfterAcquire": 1}
cold resolve phase {"videoId": "iaREBWprmvg", "purpose": "live-auth", "engine": "fg-auth-1", "phase": "webpage", "elapsedSeconds": 0.204}
INFO:     10.26.104.132:0 - "POST /prefetch/EUy6Z6xxpAU?intent=1 HTTP/1.1" 202 Accepted
cold resolve phase {"videoId": "iaREBWprmvg", "purpose": "live-pot", "engine": "fg-pot-1", "phase": "webpage", "elapsedSeconds": 0.998}
