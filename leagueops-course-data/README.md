# LeagueOps Course Data

Course lists for the Golf Course Data Agent. Used as file resources when running
Managed Agent sessions to populate the LeagueOps Supabase database.

## Structure

```
course-lists/
  minnesota/
    twin-cities-batch-1-minneapolis-stpaul.txt
    twin-cities-batch-2-north-suburbs.txt
    twin-cities-batch-3-west-suburbs.txt
    twin-cities-batch-4-south-suburbs.txt
    twin-cities-batch-5-east-metro.txt
  wisconsin/        (future)
  national/         (future)
agent-configs/
  golf-course-data-agent.yaml   ← full agent YAML config
docs/
  session-prompts.md            ← copy-paste prompts for each batch session
  progress-tracker.md           ← track which batches have been run
```

## How to Use

1. Upload the relevant batch `.txt` file as a file resource when starting
   a Managed Agent session in the Claude Console
2. Use the session prompt from `docs/session-prompts.md` for that batch
3. The agent reads the file, skips courses already in Supabase, and writes new ones
4. Update `docs/progress-tracker.md` after each session

## Adding New Regions

1. Create a new `.txt` file in the appropriate state folder
2. Follow the same format: `Course Name | City, STATE | Type | Holes`
3. Add batch session prompts to `docs/session-prompts.md`
4. Commit and push

## Course List Format

```
Course Name | City, MN | Public/Private/Semi-Private | 9/18
```

Lines beginning with `#` are comments and ignored by the agent.
