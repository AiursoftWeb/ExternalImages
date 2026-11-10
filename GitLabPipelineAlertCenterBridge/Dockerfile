FROM --platform=$BUILDPLATFORM hub.aiursoft.com/aiursoft/internalimages/dotnet AS build
WORKDIR /src
ARG NUGET_LOCKED_MODE=false
COPY *.csproj nuget.config packages.lock.json* ./
RUN if [ "$NUGET_LOCKED_MODE" = true ]; then test -f packages.lock.json; fi \
    && dotnet restore --use-lock-file --configfile nuget.config -p:RestoreLockedMode="$NUGET_LOCKED_MODE"
COPY *.cs ./
RUN dotnet publish -c Release --no-restore -o /app /p:UseAppHost=false

FROM hub.aiursoft.com/aiursoft/internalimages/dotnetonlyruntime
WORKDIR /app
COPY --from=build /app ./
RUN mkdir -p /data && chown 1654:1654 /data && chmod 700 /data
USER 1654:1654
HEALTHCHECK --interval=60s --timeout=10s --start-period=15m --retries=3 CMD ["dotnet", "Bridge.dll", "--healthcheck"]
ENTRYPOINT ["dotnet", "Bridge.dll"]
