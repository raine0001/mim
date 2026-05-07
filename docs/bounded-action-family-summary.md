# Bounded Action Family Summary

| action family name | route | bounded intent | proof status | hardened enough |
| --- | --- | --- | --- | --- |
| TOD status | goal_system | dispatch one bounded TOD status request and surface TOD's result | bridge request-id continuity, Enter submit, repeated-turn continuity, reload continuity, clear-button continuity | yes |
| current-objective summary | goal_system | dispatch one bounded TOD current-objective summary request and surface TOD's result | bridge request-id continuity, Enter submit, repeated-turn continuity, reload continuity, clear-button continuity | yes |
| recent-changes | goal_system | dispatch one bounded TOD recent-changes summary request and surface TOD's result | bridge request-id continuity, MIM text-chat metadata shape, exact browser reply rendering, Enter submit, repeated-turn continuity, reload continuity, clear-button continuity | yes |
| bridge-warning explanation | goal_system | dispatch one bounded TOD bridge-warning explanation request and surface TOD's result | bridge request-id continuity, Enter submit, repeated-turn continuity, reload continuity, clear-button continuity | yes |
| bridge-warning next-step | goal_system | dispatch one bounded TOD bridge-warning next-step recommendation request and surface TOD's result | bridge request-id continuity, Enter submit, repeated-turn continuity, reload continuity, clear-button continuity | yes |

Next candidate action family for future authorization: none currently defined on the bounded /mim browser surface.