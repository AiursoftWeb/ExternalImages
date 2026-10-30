FROM --platform=$BUILDPLATFORM hub.aiursoft.com/aiursoft/internalimages/dotnet AS build
WORKDIR /src
COPY *.csproj ./
RUN dotnet restore
COPY *.cs ./
RUN dotnet publish -c Release --no-restore -o /app /p:UseAppHost=false

FROM hub.aiursoft.com/aiursoft/internalimages/dotnetonlyruntime
WORKDIR /app
COPY --from=build /app ./
RUN mkdir -p /data && chown 1654:1654 /data && chmod 700 /data
USER 1654:1654
HEALTHCHECK --interval=60s --timeout=10s --start-period=3m --retries=3 CMD ["dotnet", "Bridge.dll", "--healthcheck"]
ENTRYPOINT ["dotnet", "Bridge.dll"]
